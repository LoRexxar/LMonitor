(() => {
  'use strict';
  const header=document.querySelector('.portal-header');
  if(header){const offset=()=>document.body.style.setProperty('--cg-header-height',`${Math.ceil(header.getBoundingClientRect().height)}px`);offset();new ResizeObserver(offset).observe(header);}
  const form = document.getElementById('cg-filters');
  if (form) {
    const cards = [...document.querySelectorAll('.cg-guide-card')], grid = document.getElementById('cg-guide-grid');
    const classButtons = [...document.querySelectorAll('[data-cg-class]')], tagButtons = [...document.querySelectorAll('[data-cg-tag]')];
    const specOptions = [...form.elements.spec.options].map(option => option.cloneNode(true));
    let selectedClass = '', selectedTags = new Set();
    const setSpecs = () => {
      const current = form.elements.spec.value;
      form.elements.spec.replaceChildren(...specOptions.filter(o => !o.value || !selectedClass || o.dataset.class === selectedClass).map(o => o.cloneNode(true)));
      form.elements.spec.value = [...form.elements.spec.options].some(o => o.value === current) ? current : '';
    };
    function filter(writeUrl = true) {
      const q = form.elements.q.value.trim().toLocaleLowerCase(), spec = form.elements.spec.value, role = form.elements.role.value;
      let count = 0;
      cards.forEach(card => {
        const tags = [...card.querySelectorAll('.cg-tag')].map(t => t.textContent);
        const show = (!selectedClass || card.dataset.class === selectedClass) && (!spec || card.dataset.spec === spec) && (!role || card.dataset.role === role) && (!q || card.textContent.toLocaleLowerCase().includes(q)) && [...selectedTags].every(t => tags.includes(t));
        card.hidden = !show; count += Number(show);
      });
      [...cards].sort((a,b) => form.elements.sort.value === 'updated' ? b.dataset.updated.localeCompare(a.dataset.updated) : cards.indexOf(a)-cards.indexOf(b)).forEach(c => grid.append(c));
      classButtons.forEach(b => { const on = b.dataset.cgClass === selectedClass; b.classList.toggle('is-selected',on); b.setAttribute('aria-pressed',String(on)); });
      tagButtons.forEach(b => b.setAttribute('aria-pressed',String(selectedTags.has(b.dataset.cgTag))));
      document.getElementById('cg-result-count').textContent = `${count} 篇`;
      document.getElementById('cg-result-title').textContent = spec ? form.elements.spec.selectedOptions[0].textContent + '攻略' : selectedClass ? classButtons.find(b => b.dataset.cgClass === selectedClass).dataset.label + '攻略' : '全部攻略';
      document.getElementById('cg-empty').hidden = count > 0;
      document.getElementById('cg-clear').hidden = !selectedClass && !spec && !role && !q && !selectedTags.size;
      if (writeUrl) {
        const params = new URLSearchParams(); if (selectedClass) params.set('class',selectedClass);
        for(const name of ['q','spec','role','sort']) if(form.elements[name].value && !(name==='sort' && form.elements[name].value==='class')) params.set(name,form.elements[name].value);
        selectedTags.forEach(t => params.append('tag',t));
        history.replaceState(null,'',location.pathname + (params.size ? '?' + params : ''));
      }
    }
    function readUrl() {
      const params = new URLSearchParams(location.search);
      selectedClass = classButtons.some(b => b.dataset.cgClass === params.get('class')) ? params.get('class') : '';
      setSpecs();
      for(const key of ['q','spec','role','sort']) form.elements[key].value = params.get(key) || (key==='sort'?'class':'');
      selectedTags = new Set(params.getAll('tag').filter(t=>tagButtons.some(b=>b.dataset.cgTag===t)));
      filter(false);
    }
    function reset() { selectedClass=''; selectedTags.clear(); form.reset(); setSpecs(); filter(); }
    classButtons.forEach(b=>b.addEventListener('click',()=>{selectedClass=b.dataset.cgClass;setSpecs();filter();}));
    tagButtons.forEach(b=>b.addEventListener('click',()=>{const tag=b.dataset.cgTag;selectedTags.has(tag)?selectedTags.delete(tag):selectedTags.add(tag);filter();}));
    form.addEventListener('submit',e=>{e.preventDefault();filter();});form.addEventListener('input',()=>filter());
    document.querySelectorAll('#cg-clear,[data-cg-reset]').forEach(b=>b.addEventListener('click',reset));
    window.addEventListener('popstate',readUrl);readUrl();
  }
  const article = document.querySelector('.cg-article-body');
  if (article) {
    const links=[...document.querySelectorAll('[data-cg-heading]')], toc=document.getElementById('cg-toc-panel');
    const small=matchMedia('(max-width:760px)'); const collapse=()=>{toc.open=!small.matches;};collapse();small.addEventListener('change',collapse);
    function revealHash() {
      let id;try{id=decodeURIComponent(location.hash.slice(1));}catch{return;}
      const target=document.getElementById(id);if(!target||(!article.contains(target)&&id!=='cg-main'))return;
      let branch=links.find(link=>link.dataset.cgHeading===id)?.closest('.cg-toc-branch');
      while(branch){branch.open=true;branch=branch.parentElement.closest('.cg-toc-branch');}
      window.revealGuideHeading(target);
    }
    document.querySelector('.cg-reader').addEventListener('click',e=>{
      const link=e.target.closest('a[href]');
      if(!link||e.defaultPrevented||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
      if(link.origin!==location.origin||link.pathname!==location.pathname||link.search!==location.search||!link.hash)return;
      let id;try{id=decodeURIComponent(link.hash.slice(1));}catch{return;}
      const target=document.getElementById(id);if(!target||(!article.contains(target)&&id!=='cg-main'))return;
      e.preventDefault();if(small.matches)toc.open=false;
      if(location.hash!==link.hash)history.pushState(null,'',link.hash);
      revealHash();
    });
    window.addEventListener('hashchange',revealHash);if(location.hash)requestAnimationFrame(revealHash);
    const targets=links.map(link=>({link,node:document.getElementById(link.dataset.cgHeading)})).filter(row=>row.node);
    let positions=[], activeLink=null, activeChapter=null, scheduled=false, measuring=false;
    const threshold=()=> (header?.getBoundingClientRect().height||64)+(small.matches?76:28);
    function measure() {
      measuring=false;
      positions=targets.filter(row=>row.node.getClientRects().length).map(row=>({...row,top:row.node.getBoundingClientRect().top+window.scrollY})).sort((a,b)=>a.top-b.top);
      progress();
    }
    function scheduleMeasure(){if(!measuring){measuring=true;requestAnimationFrame(measure);}}
    function progress() {
      scheduled=false;
      const edge=window.scrollY+threshold()+2;
      let low=0,high=positions.length-1,index=0;
      while(low<=high){const mid=(low+high)>>1;if(positions[mid].top<=edge){index=mid;low=mid+1;}else high=mid-1;}
      const current=positions[index]?.link;
      if(current&&current!==activeLink){
        activeLink?.removeAttribute('aria-current');current.setAttribute('aria-current','true');activeLink=current;
        let branch=current.closest('.cg-toc-branch');
        while(branch?.parentElement.closest('.cg-toc-branch'))branch=branch.parentElement.closest('.cg-toc-branch');
        if(branch!==activeChapter){activeChapter?.classList.remove('is-current-chapter');branch?.classList.add('is-current-chapter');activeChapter=branch;}
      }
      const rect=article.getBoundingClientRect(), total=Math.max(1,rect.height-innerHeight+threshold());
      document.getElementById('cg-reading-progress').textContent=`${Math.round(Math.max(0,Math.min(1,(threshold()-rect.top)/total))*100)}%`;
    }
    document.addEventListener('scroll',()=>{if(!scheduled){scheduled=true;requestAnimationFrame(progress);}},{passive:true,capture:true});
    article.addEventListener('toggle',scheduleMeasure,true);
    article.addEventListener('load',scheduleMeasure,true);
    new ResizeObserver(scheduleMeasure).observe(article);
    window.addEventListener('resize',scheduleMeasure);
    measure();
  }
  // 来源封面不可用时保留职业底图，避免破图占位影响阅读。
  document.querySelectorAll('.cg-card-cover>img,.cg-hero-cover').forEach(img=>{const fallback=()=>{img.hidden=true;};img.addEventListener('error',fallback);if(img.complete&&!img.naturalWidth)fallback();});
  document.querySelectorAll('.cg-title-line>img,.cg-hero-spec img,.cg-cover-spec img,.guide-ref img,.cg-class-symbol img').forEach(img=>{
    const recover=()=>{
      if(!img.dataset.cgFallback){
        img.dataset.cgFallback='1';
        const filename=new URL(img.src,location.href).pathname.split('/').pop();
        if(/^[a-z0-9_]+\.(jpg|png)$/i.test(filename)){img.src='https://render.worldofwarcraft.com/us/icons/56/'+filename;return;}
      }
      img.hidden=true;
    };
    img.addEventListener('error',recover);if(img.complete&&!img.naturalWidth)recover();
  });
})();
