// 扫描只保留行首和字节区间，长事件的原文由 File.slice 在上传时读取。
const blockSize = 4 * 1024 * 1024;
const csv = line => Array.from(line.matchAll(/(?:^|,)("(?:[^"]|"")*"|[^,]*)/g),
    match => match[1].replace(/^"|"$/g, '').replace(/""/g, '"'));
const raids = new Set(['3','4','5','6','7','9','14','15','16','17','33','151','220']);
const dungeons = new Set(['1','2','8','23','24','150']);
const keyLevel = value => /^[1-9][0-9]*$/.test(value) ? value : '';
function bossKind(difficulty) {
    if (raids.has(difficulty)) return '团本首领';
    if (dungeons.has(difficulty)) return difficulty === '8' ? '大秘境首领' : '地下城首领';
}
const damage = new Set(['SPELL_DAMAGE','SPELL_PERIODIC_DAMAGE','RANGE_DAMAGE','SWING_DAMAGE','ENVIRONMENTAL_DAMAGE','DAMAGE_SHIELD','DAMAGE_SPLIT']);
function millis(stamp) {
    // 三位毫秒后可直接接正时区，如 .2308 等同于 .230+8。
    const parts = stamp.match(/^(\d+)\/(\d+)(?:\/(\d+))? (\d+):(\d+):(\d+)\.(\d{3})(?:([+-]?)(\d{1,2})(?::(\d{2}))?)?$/);
    if (!parts) return NaN;
    const minutes = (parts[8] === '-' ? -1 : 1) * (Number(parts[9] || 0)*60 + Number(parts[10] || 0));
    return Date.UTC(Number(parts[3] || 2000),+parts[1]-1,+parts[2],+parts[4],+parts[5],+parts[6],+parts[7]) - minutes*60000;
}

async function scan(file, legacy) {
    const decoder = new TextDecoder(), prefix = new Uint8Array(2048), scenes = [];
    const people = new Map(), blockHashes = [];
    let start = 0, used = 0, header = null, current = null, zone = null, opening = false;
    let headerText = '', pendingHeader = null;
    const previousPrefix = new Uint8Array(52);
    const headerGap = 300000;
    function finish(end, result) {
        if (current && current.header && end > current.start && (current.kind !== '地下城' || current.hasCombat)) {
            current.end = end;
            current.result = result;
            delete current.candidates;
            delete current.hasCombat;
            scenes.push(current);
        }
        current = null;
        opening = false;
    }
    function finishPartial(end, boundary) {
        if (current) current.boundary = boundary;
        finish(end, current?.kind === '大秘境' ? '未结算' : '部分记录');
    }
    function begin(kind, name, id, difficulty, stamp, map) {
        finishPartial(start, '下一场开始');
        current = {kind, name, id, difficulty, stamp, map, start, header, context: [], candidates: new Map(people)};
        opening = kind === '大秘境';
    }
    function line(end) {
        // 根据时间戳后的前几个字节排除大量伤害、治疗和光环事件。
        const space = prefix.subarray(0, Math.min(used, 52)).indexOf(32, 10);
        let at = space;
        while (at >= 0 && at + 1 < used && !(prefix[at] === 32 && prefix[at+1] === 32)) at++;
        if (at < 0 || at+2 >= used) {
            if (pendingHeader) {
                finishPartial(pendingHeader.start, '日志会话切换');
                pendingHeader = zone = null;
            }
            return;
        }
        const first = prefix[at+2];
        if (current?.kind === '地下城' && !current.hasCombat) {
            const event = decoder.decode(prefix.subarray(at+2, Math.min(used,at+36))).split(',')[0];
            if (damage.has(event)) {
                const [,src,,,,dst] = csv(decoder.decode(prefix.subarray(at+2,used)));
                const player = /^(Player|Pet)-/, enemy = /^(Creature|Vehicle)-/;
                current.hasCombat = (player.test(src) && enemy.test(dst)) || (enemy.test(src) && player.test(dst));
            }
        }
        if (pendingHeader) {
            const fields = csv(decoder.decode(prefix.subarray(at+2,used)).trimEnd());
            const delay = millis(decoder.decode(prefix.subarray(0,at))) - pendingHeader.ms;
            const sameKey = (fields[0] === 'ZONE_CHANGE' && fields[1] === current.map && fields[3] === '8') ||
                (fields[0] === 'ENCOUNTER_START' && fields[5] === current.map && fields[3] === '8');
            if ((fields[0] === 'MAP_CHANGE' || fields[0] === 'COMBATANT_INFO') &&
                pendingHeader.rows < 32 && end - pendingHeader.end <= 2 * 1024 * 1024) {
                pendingHeader.rows++;
            } else {
                if (!sameKey || !(delay >= -1000 && delay <= headerGap)) {
                    finishPartial(pendingHeader.start, '日志会话切换');
                    zone = null;
                }
                pendingHeader = null;
            }
        }
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
            const signature = text.slice(sep+2);
            header = version >= 0 && fields[version+1] === '1' ? [start,end] : null;
            const delay = opening ? millis(stamp) - millis(current.stamp) : NaN;
            const gap = millis(stamp) - millis(decoder.decode(previousPrefix).split('  ')[0]);
            if (opening && header && delay >= 0 && delay <= 5000) {
                current.header = header;
                current.context = [];
                current.candidates.clear();
                // 版本头前的开场队伍信息属于本次会话。
                for (const [guid, bounds] of people) {
                    if (legacy || bounds[0] < current.start) people.delete(guid);
                }
            } else if (header && signature === headerText && current?.kind === '大秘境' && gap >= 0 && gap <= headerGap) {
                pendingHeader = {start, end, rows:0, ms:millis(stamp)};
                people.clear();
            } else {
                finishPartial(start,'日志会话切换');
                zone = null;
                people.clear();
            }
            opening = false;
            headerText = signature;
        } else if (kind === 'COMBATANT_INFO') {
            if (fields[1]?.startsWith('Player-')) people.set(fields[1],[start,end]);
        } else if (kind === 'CHALLENGE_MODE_START') {
            begin('大秘境', fields[1], fields[2], keyLevel(fields[4]), stamp, fields[2]);
            current.bosses = [];
        } else if (kind === 'CHALLENGE_MODE_END') {
            if (!['0','1'].includes(fields[2]) || !/^[0-9]+$/.test(fields[4])) return;
            if (current?.kind === '大秘境' && current.id === fields[1]) {
                if (fields[2] === '0' && fields[3] === '0' && fields[4] === '0') {
                    finishPartial(start, '钥匙重置');
                } else {
                    const endLevel = keyLevel(fields[3]);
                    if (endLevel && current.difficulty && endLevel !== current.difficulty) {
                        current.levelNote = '开场 +' + current.difficulty + '，结束 +' + endLevel;
                        current.difficulty = '冲突';
                    } else current.difficulty ||= endLevel;
                    finish(end,fields[2] === '1' ? '已完成' : '未完成');
                }
            } else if (current?.kind === '大秘境' && /^[1-9][0-9]*$/.test(fields[1])) {
                finishPartial(start, '其他副本的钥匙标记');
            } else if (current?.kind.endsWith('首领')) finishPartial(start, '钥匙结束标记');
        } else if (kind === 'ZONE_CHANGE') {
            if (current && ((current.map && current.map !== fields[1]) ||
                (current.kind === '大秘境' && fields[3] && fields[3] !== '8') ||
                (current.kind !== '大秘境' && fields[3] && fields[3] !== current.difficulty))) finishPartial(start,'离开副本');
            zone = {id:fields[1], difficulty:fields[3]};
            if (!current && header && dungeons.has(fields[3]) && fields[3] !== '8')
                begin('地下城', fields[2], fields[1], fields[3], stamp, fields[1]);
        } else if (kind === 'ENCOUNTER_START') {
            if (current?.kind === '大秘境' || current?.kind === '地下城') {
                if ((!fields[5] || fields[5] === current.map) &&
                    fields[3] === (current.kind === '大秘境' ? '8' : current.difficulty)) {
                    current.hasCombat = true;
                    if (current.bosses) current.bosses.push(fields[2]);
                    return;
                }
                finishPartial(start, '下一场开始');
            }
            if (current?.kind.endsWith('首领')) finishPartial(start, '下一场开始');
            const encounter = bossKind(fields[3]);
            if (encounter) begin(encounter, fields[2], fields[1], fields[3], stamp, fields[5] || zone?.id);
        } else if (kind === 'ENCOUNTER_END' && ['0','1'].includes(fields[5])) {
            if (current?.kind === '大秘境' || current?.kind === '地下城') {
                if (fields[3] === (current.kind === '大秘境' ? '8' : current.difficulty)) {
                    current.hasCombat = true;
                    return;
                }
                finishPartial(start, '离开副本');
            } else if (current && (current.id !== fields[1] || current.difficulty !== fields[3])) {
                finishPartial(start, '其他首领的结束标记');
            }
            const encounter = bossKind(fields[3]);
            if (!encounter || !header) return;
            const outcome = fields[5] === '1' ? '击杀' : '未击杀';
            const endOnly = !current;
            if (endOnly) begin(encounter, fields[2], fields[1], fields[3], stamp, zone?.id);
            finish(end, endOnly ? outcome + '（仅记录结尾）' : outcome);
        }
    }
    for (let offset = 0; offset < file.size; offset += blockSize) {
        const bytes = new Uint8Array(await file.slice(offset, offset+blockSize).arrayBuffer());
        // Hash the already-read blocks; no second file pass or whole-file buffer.
        if (crypto.subtle) blockHashes.push(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)));
        for (let pos = 0; pos < bytes.length;) {
            const nl = bytes.indexOf(10,pos), stop = nl < 0 ? bytes.length : nl;
            const n = Math.min(stop-pos, prefix.length-used);
            if (n) { prefix.set(bytes.subarray(pos,pos+n),used); used += n; }
            if (nl < 0) break;
            const end = offset+nl+1;
            line(end);
            previousPrefix.set(prefix.subarray(0,previousPrefix.length));
            start=end; used=0; pos=nl+1;
        }
        postMessage({progress: Math.min(file.size,offset+bytes.length)});
    }
    if (start < file.size) line(file.size);
    finishPartial(pendingHeader?.start ?? file.size, pendingHeader ? '日志会话切换' : '文件结束');
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
    let sourceHash;
    if (crypto.subtle) {
        const hashes = new Uint8Array(blockHashes.length * 32);
        blockHashes.forEach((hash, i) => hashes.set(hash, i * 32));
        const digest = await crypto.subtle.digest('SHA-256', hashes);
        sourceHash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
    }
    postMessage({scenes, sourceHash, unterminated: start < file.size});
}
self.onmessage = ({data}) => scan(data.file, data.legacy).catch(() => postMessage({error:'无法读取日志，请重新选择文件。'}));
