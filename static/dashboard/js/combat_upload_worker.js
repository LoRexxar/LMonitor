// 扫描只保留行首和字节区间，长事件的原文由 File.slice 在上传时读取。
const blockSize = 4 * 1024 * 1024;
const csv = line => Array.from(line.matchAll(/(?:^|,)("(?:[^"]|"")*"|[^,]*)/g),
    match => match[1].replace(/^"|"$/g, '').replace(/""/g, '"'));
const raids = new Set(['3','4','5','6','7','9','14','15','16','17','33','151','220']);
const dungeons = new Set(['1','2','8','23','24','150']);
function millis(stamp) {
    const parts = stamp.match(/^(\d+)\/(\d+)(?:\/(\d+))? (\d+):(\d+):(\d+)\.(\d+)/);
    return parts ? Date.UTC(Number(parts[3] || 2000),+parts[1]-1,+parts[2],+parts[4],+parts[5],+parts[6],+parts[7]) : NaN;
}

async function scan(file) {
    const decoder = new TextDecoder(), prefix = new Uint8Array(2048), scenes = [];
    const people = new Map();
    let start = 0, used = 0, header = null, current = null, zone = null, opening = false;
    function finish(end, result) {
        if (current && current.header && end > current.start) {
            current.end = end;
            current.result = result;
            delete current.candidates;
            scenes.push(current);
        }
        current = null;
        opening = false;
    }
    function begin(kind, name, id, difficulty, stamp, map) {
        finish(start, '结束标记缺失');
        current = {kind, name, id, difficulty, stamp, map, start, header, context: [], candidates: new Map(people)};
        opening = kind === '大秘境';
    }
    function line(end) {
        // 根据时间戳后的前几个字节排除大量伤害、治疗和光环事件。
        const space = prefix.subarray(0, Math.min(used, 52)).indexOf(32, 10);
        let at = space;
        while (at >= 0 && at + 1 < used && !(prefix[at] === 32 && prefix[at+1] === 32)) at++;
        if (at < 0 || at+2 >= used) return;
        const first = prefix[at+2];
        if (current?.candidates.size) {
            const leading = decoder.decode(prefix.subarray(at+2, Math.min(used,at+514)));
            for (const match of leading.matchAll(/(?:^|,)(Player-[^,\s]+)(?=,|$)/g)) {
                if (current.candidates.has(match[1])) {
                    if (!leading.startsWith('COMBATANT_INFO,')) current.context.push(current.candidates.get(match[1]));
                    current.candidates.delete(match[1]);
                }
            }
        }
        if (first !== 67 && first !== 69 && first !== 90) { opening = false; return; }
        const text = decoder.decode(prefix.subarray(0,used)).trimEnd();
        const sep = text.indexOf('  ');
        if (sep < 0) return;
        const fields = csv(text.slice(sep+2)), kind = fields[0], stamp = text.slice(0,sep);
        if (kind === 'COMBAT_LOG_VERSION') {
            const version = fields.indexOf('PROJECT_ID');
            header = version >= 0 && fields[version+1] === '1' ? [start,end] : null;
            const delay = opening ? millis(stamp) - millis(current.stamp) : NaN;
            if (opening && header && delay >= 0 && delay <= 5000) {
                current.header = header;
                current.context = [];
                current.candidates.clear();
            } else {
                finish(start,'结束标记缺失');
                zone = null;
            }
            opening = false;
            people.clear();
        } else if (kind === 'COMBATANT_INFO') {
            if (fields[1]?.startsWith('Player-')) people.set(fields[1],[start,end]);
        } else if (kind === 'CHALLENGE_MODE_START') {
            begin('大秘境', fields[1], fields[2], fields[4], stamp, fields[2]);
        } else if (kind === 'CHALLENGE_MODE_END') {
            if (current?.kind === '大秘境' && current.id === fields[1]) finish(end,fields[2] === '1' ? '已完成' : '未完成');
        } else if (kind === 'ZONE_CHANGE') {
            if (current && current.map && current.map !== fields[1]) finish(start,current.kind === '地下城' ? '离开副本' : '结束标记缺失');
            zone = {id:fields[1], difficulty:fields[3]};
            if (!current && header && dungeons.has(fields[3]) && fields[3] !== '8')
                begin('地下城', fields[2], fields[1], fields[3], stamp, fields[1]);
        } else if (kind === 'ENCOUNTER_START') {
            if (current?.kind === '大秘境' || current?.kind === '地下城') {
                if ((!fields[5] || fields[5] === current.map) && dungeons.has(fields[3])) return;
                finish(start, '结束标记缺失');
            }
            if (raids.has(fields[3]) || dungeons.has(fields[3]))
                begin(raids.has(fields[3]) ? '团本首领' : '地下城首领', fields[2], fields[1], fields[3], stamp, fields[5] || zone?.id);
        } else if (kind === 'ENCOUNTER_END' && current?.kind.endsWith('首领') && current.id === fields[1]) {
            finish(end, fields[5] === '1' ? '击杀' : '未击杀');
        }
    }
    for (let offset = 0; offset < file.size; offset += blockSize) {
        const bytes = new Uint8Array(await file.slice(offset, offset+blockSize).arrayBuffer());
        for (let pos = 0; pos < bytes.length;) {
            const nl = bytes.indexOf(10,pos), stop = nl < 0 ? bytes.length : nl;
            const n = Math.min(stop-pos, prefix.length-used);
            if (n) { prefix.set(bytes.subarray(pos,pos+n),used); used += n; }
            if (nl < 0) break;
            const end = offset+nl+1;
            line(end); start=end; used=0; pos=nl+1;
        }
        postMessage({progress: Math.min(file.size,offset+bytes.length)});
    }
    if (start < file.size) line(file.size);
    finish(file.size, '结束标记缺失');
    for (const scene of scenes) {
        const [lo,hi] = scene.header;
        scene.ranges = [scene.header, ...scene.context.filter(r=>r[1]<=scene.start).sort((a,b)=>a[0]-b[0])];
        if (lo >= scene.start && hi <= scene.end) {
            scene.ranges.push([scene.start,lo], [hi,scene.end]);
        } else scene.ranges.push([scene.start,scene.end]);
        scene.ranges = scene.ranges.filter(([a,b])=>b>a);
        scene.size = scene.ranges.reduce((sum,[a,b])=>sum+b-a,0);
        delete scene.header;
        delete scene.context;
    }
    postMessage({scenes});
}
self.onmessage = ({data}) => scan(data).catch(() => postMessage({error:'无法读取日志，请重新选择文件。'}));
