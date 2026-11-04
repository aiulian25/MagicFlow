/** FEAT-004: real DOM/Blob previews, shared transforms and lifecycle in Chrome. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
const viewer=readFileSync(new URL('../app/static/compare.js',import.meta.url),'utf8');
const css=readFileSync(process.env.MAGICFLOW_CSS_SOURCE || new URL('../app/static/vendor/tailwind.css',import.meta.url),'utf8');

test('comparison aligns views, handles unavailable sources and releases bounded previews',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-compare-'));
 try{
 const helper=html.slice(html.indexOf('let activeModal ='),html.indexOf('const reconvertModal ='));
 const adapters=html.slice(html.indexOf('// Comparison adapters'),html.lastIndexOf('</script>'));
 const program=`
 const failures=[],check=(ok,message)=>{if(!ok)failures.push(message);};
 const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
 const wait=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await tick();}throw new Error('Preview did not settle');};
 (async()=>{try{
 const canvas=document.createElement('canvas');canvas.width=4;canvas.height=2;canvas.getContext('2d').fillRect(0,0,2,2);
 const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
 const file=new File([blob],'original "&.png',{type:'image/png'});
 const state={comparisonSource:file,pendingDownload:{filename:'result.png'},convertFiles:[{status:'done',file,comparisonSource:file,resultName:'converted.png'}]};
 const selectedFiles=new Set(['first.png','second.png']);
 let pendingReconvertResult={comparisonSource:'saved-source.png',output_filename:'temporary.png'};
 const reconvertModal=document.getElementById('reconvert-modal');
 const closeReconvert=()=>{endModal(reconvertModal);reconvertModal.style.display='none';};
 let slow=null;const requests=[],live=new Set();
 const originalCreate=URL.createObjectURL.bind(URL),originalRevoke=URL.revokeObjectURL.bind(URL);
 URL.createObjectURL=value=>{const url=originalCreate(value);live.add(url);return url;};
 URL.revokeObjectURL=url=>{live.delete(url);originalRevoke(url);};
 const response=()=>new Response(blob,{headers:{'X-Compare-Width':'4000','X-Compare-Height':'2000','X-Compare-Bytes':'80000','X-Compare-Preview-Width':'4','X-Compare-Preview-Height':'2'}});
 window.fetch=async(url,options={})=>{
  requests.push([url,options]);
  if(url.includes('missing'))return new Response(JSON.stringify({detail:'File not found'}),{status:404});
  if(url.includes('slow'))return new Promise(resolve=>{slow=()=>resolve(response());});
  return response();
 };
 ${adapters}
 document.getElementById('download-section').hidden=false;
 const opener=document.getElementById('compare-result-btn');opener.focus();opener.click();
 const root=document.getElementById('compare-modal'),images=[...root.querySelectorAll('img')],surfaces=[...root.querySelectorAll('.compare-surface')];
 await wait(()=>images.every(img=>!img.hidden));
 check(root.style.display==='flex' && document.querySelector('main').inert,'focus boundary');
 check(document.activeElement===document.getElementById('compare-close'),'initial close focus');
 const tabs=modalFocusables(root);tabs.at(-1).focus();tabs.at(-1).dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}));check(document.activeElement===tabs[0],'comparison traps keyboard focus');
 check(requests[0][0]==='/compare-preview' && requests[0][1].body.get('file').name===file.name,'original sent only on comparison');
 check(root.querySelector('h3').textContent.includes(file.name),'filename is literal text');
 check(root.textContent.includes('Not pixel-exact') && root.textContent.includes('not synchronized'),'preview limits and timing explicit');
 check(root.textContent.includes('4000 × 2000') && root.textContent.includes('80000 bytes'),'source metadata');
 check(images[0].style.width===images[1].style.width,'equal geometry fits equally');
 const zoom=document.getElementById('compare-zoom');zoom.value='2';zoom.dispatchEvent(new Event('input'));
 surfaces[0].dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
 check(images[0].style.transform===images[1].style.transform && images[0].style.transform.includes('scale(2)'),'shared pan/zoom');
 const source=images[0].src,bg=document.getElementById('compare-background');bg.value='black';bg.dispatchEvent(new Event('change'));
 check(surfaces.every(el=>getComputedStyle(el).backgroundColor==='rgb(0, 0, 0)') && images[0].src===source,'background only affects viewing');
 document.getElementById('compare-reset').click();check(zoom.value==='1','reset');
 document.getElementById('compare-close').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 check(root.style.display==='none' && !document.querySelector('main').inert && live.size===0,'Escape releases preview URLs and inert');
 check(document.activeElement===opener,'focus restored');
 state.comparisonSource=null;opener.click();await wait(()=>!images[1].hidden);
 check(root.textContent.includes('Original no longer available'),'missing original fallback');MagicFlowCompare.close();
 const selected=document.getElementById('compare-selected-btn');selected.disabled=false;selected.click();await wait(()=>images.every(img=>!img.hidden));
 check(requests.slice(-2).every(([url])=>url.startsWith('/compare-preview/')),'two saved outputs use read-only previews');MagicFlowCompare.close();
 const button=document.createElement('button');button.dataset.convertCompare='0';document.body.append(button);button.click();await wait(()=>images.every(img=>!img.hidden));
 check(root.textContent.includes('converted.png'),'Convert result entry point');MagicFlowCompare.close();
 reconvertModal.style.display='flex';beginModal(reconvertModal,closeReconvert,document.getElementById('reconvert-compare'));
 document.getElementById('reconvert-compare').click();await wait(()=>images.every(img=>!img.hidden));MagicFlowCompare.close();
 check(reconvertModal.style.display==='flex' && pendingReconvertResult.output_filename==='temporary.png','temporary result remains reachable after comparison');
 closeReconvert();
 MagicFlowCompare.open({name:'missing.png'},{name:'result.png'});await wait(()=>root.textContent.includes('File not found'));
 check(!root.querySelector('[data-compare-status]').hidden,'missing file has retry guidance');MagicFlowCompare.close();
 MagicFlowCompare.open({name:'slow.png'},{name:'result.png'});await wait(()=>slow!==null);MagicFlowCompare.close();slow();await tick();await tick();
 check(live.size===0 && root.style.display==='none','late response cannot reopen or leak a URL');
 check(requests.every(([url])=>url.startsWith('/compare-preview')),'comparison never deletes or downloads originals');
 }catch(error){failures.push(error.stack || error.message);}
 const result=document.createElement('pre');result.id='compare-test-result';result.textContent=JSON.stringify(failures);document.body.append(result);
 })();`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'')
  .replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+helper+viewer+program+'</script></body>');
 const file=join(work,'fixture.html');writeFileSync(file,fixture);
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--virtual-time-budget=5000','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:10*1024*1024});
 assert.equal(run.status,0,run.stderr);
 const result=run.stdout.match(/<pre id="compare-test-result">([^<]+)<\/pre>/);assert.ok(result,'comparison browser scenario completed');
 assert.deepEqual(JSON.parse(result[1]),[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});

for (const width of [320,1280]) test('comparison and saved-card controls fit '+width+'px',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-compare-layout-'));
 try{
 const start=html.indexOf('function renderSavedFiles(');
 const render=html.slice(start,html.indexOf('\n}',start)+2);
 const script=`
 const state={},selectedFiles=new Set(),EXPLODE_EXTS=new Set(['.png']);
 const escapeHtml=s=>s,formatBytes=n=>String(n),recipeMetaLine=()=>'',reconvertBlockInfo=()=>({blocked:false,title:''}),syncFileSelectionUI=()=>{};
 ${render}
 for(const view of ['dashboard','files','settings'])document.getElementById('view-'+view).hidden=view!=='files';
 renderSavedFiles([{filename:'a-very-long-stored-output-name.png',size:12345,modified:1}]);
 const failures=[];
 for(const card of document.querySelectorAll('#saved-files-grid > div'))for(const button of card.querySelectorAll('button,a')){const b=button.getBoundingClientRect(),c=card.getBoundingClientRect();if(b.right>c.right+1 || b.left<c.left-1)failures.push('card action overflow');}
 const modal=document.getElementById('compare-modal');modal.style.display='flex';
 for(const control of modal.querySelectorAll('button,input,select,.compare-surface')){const b=control.getBoundingClientRect();if(b.left<0 || b.right>innerWidth+1)failures.push('comparison control overflow');}
 const result=document.createElement('pre');result.id='compare-layout';result.textContent=JSON.stringify(failures);document.body.append(result);`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'')
 .replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+script+'</script></body>');
 const file=join(work,'fixture.html');
 writeFileSync(file,'<iframe style="border:0;width:'+width+'px;height:900px" srcdoc="'+fixture.replaceAll('&','&amp;').replaceAll('"','&quot;')+'"></iframe><script>window.onload=()=>document.body.append(document.querySelector("iframe").contentDocument.getElementById("compare-layout").cloneNode(true));</script>');
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:10*1024*1024});
 assert.equal(run.status,0,run.stderr);const result=run.stdout.match(/<pre id="compare-layout">([^<]+)<\/pre>/);assert.ok(result);assert.deepEqual(JSON.parse(result[1]),[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});
