(() => {
    const cursor = document.getElementById('boss-cursor');
    if (!cursor) return;
    const canvas = document.getElementById('boss-chart');
    const curves = JSON.parse(document.getElementById('boss-curves')?.textContent || '[]');
    const cells = [...document.querySelectorAll('[data-cursor-hp]')];
    const visible = [...document.querySelectorAll('[data-curve-visible]')];
    const duration = Number(cursor.max);
    const left = 42, right = 18, top = 14, bottom = 25;

    function display() {
        const second = Number(cursor.value);
        cursor.form.querySelector('output').textContent = second + ' 秒';
        if (!canvas) return;
        const width = canvas.clientWidth, height = canvas.clientHeight;
        const scale = window.devicePixelRatio || 1;
        canvas.width = Math.round(width * scale);
        canvas.height = Math.round(height * scale);
        const g = canvas.getContext('2d');
        g.scale(scale, scale);
        const x = t => left + t / duration * (width-left-right);
        const y = pct => top + (100-pct) / 100 * (height-top-bottom);
        const theme = getComputedStyle(canvas);
        g.font = '12px sans-serif';
        for (const pct of [0,25,50,75,100]) {
            g.fillStyle = theme.getPropertyValue('--secondary'); g.fillText(pct+'%', 1, y(pct)+4);
            g.strokeStyle = theme.getPropertyValue('--grid');
            g.beginPath(); g.moveTo(left,y(pct)); g.lineTo(width-right,y(pct)); g.stroke();
        }
        g.fillText('0s', left, height-4);
        g.textAlign='right'; g.fillText(duration+'s', width-right, height-4);
        const baseline = new Map();
        curves.forEach((curve, index) => {
            g.strokeStyle=curve.color; g.fillStyle=curve.color; g.lineWidth=2;
            g.beginPath();
            let last, nearest;
            for (const [t,pct] of curve.points) {
                if (visible[index].checked) {
                    if (last === undefined || t-last>5) g.moveTo(x(t),y(pct));
                    else g.lineTo(x(t),y(pct));
                    g.fillRect(x(t)-1,y(pct)-1,2,2);
                }
                if (t <= second) nearest=[t,pct];
                last=t;
            }
            g.stroke();
            const known = nearest && second-nearest[0]<=5 && second<=curve.end;
            cells[index].textContent=known ? nearest[1]+'%' : '—';
            if (curve.baseline) {
                baseline.set(curve.unit, baseline.has(curve.unit) || !known ? null : nearest[1]);
            } else if (known && baseline.get(curve.unit) != null &&
                       curves.filter(c => c.number === curve.number && c.unit === curve.unit).length === 1) {
                const delta=nearest[1]-baseline.get(curve.unit);
                cells[index].textContent += ' ('+(delta>0 ? '+' : '')+delta.toFixed(1)+' 个百分点)';
            }
            cells[index].title=known ? '实测于 +'+nearest[0]+' 秒' : '此时没有临近的血量记录';
        });
        g.strokeStyle=theme.getPropertyValue('--secondary'); g.lineWidth=1; g.setLineDash([4,4]);
        g.beginPath(); g.moveTo(x(second),top); g.lineTo(x(second),height-bottom); g.stroke();
    }
    cursor.addEventListener('input',display);
    visible.forEach(input => input.addEventListener('change',display));
    if (canvas) {
        canvas.addEventListener('click', e => {
            const bounds=canvas.getBoundingClientRect();
            cursor.value=Math.round((e.clientX-bounds.left-left)/(bounds.width-left-right)*duration);
            display();
        });
        new ResizeObserver(display).observe(canvas);
        new MutationObserver(display).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme','data-portal-theme']});
        matchMedia('(prefers-color-scheme: dark)').addEventListener('change',display);
    }
    display();
})();
