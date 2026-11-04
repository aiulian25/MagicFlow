/** FEAT-013: actual PDF controls, request values and representative page raster. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
const css=readFileSync(process.env.MAGICFLOW_CSS_SOURCE || new URL('../app/static/vendor/tailwind.css',import.meta.url),'utf8');

test('PDF controls preserve drafts, serialize physical layout and preview first-page fit/crop',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-pdf-layout-'));
 try {
 const from=html.indexOf('const PDF_FIELDS ='),code=html.slice(from,html.indexOf('// ═',from));
 const persist=html.slice(html.indexOf('const PERSIST_IDS = ['),html.indexOf('function applySettings('));
 const script=`
 const failures=[],check=(ok,message)=>{if(!ok)failures.push(message);};
 const image=(color,w,h)=>{const c=document.createElement('canvas');c.width=w;c.height=h;const ctx=c.getContext('2d');ctx.fillStyle=color;ctx.fillRect(0,0,w,h);return c.toDataURL();};
 const state={files:[{objectURL:image('red',80,40)},{objectURL:image('blue',40,80)}]};let reconvertTarget=null;
 ${code}
 ${persist}
 const set=(key,value)=>{document.getElementById('setting-pdf-'+key).value=value;};
 const tick=()=>new Promise(resolve=>setTimeout(resolve,60));
 (async()=>{try{
 const format=document.getElementById('setting-format'),panel=document.getElementById('setting-pdf-panel'),canvas=document.getElementById('setting-pdf-preview');
 format.value='gif';updatePDFLayout('setting');check(panel.hidden,'PDF-only panel hidden for GIF');
 format.value='pdf';set('page-size','custom');set('width-mm','100');set('height-mm','100');set('margin-mm','10');
 updatePDFLayout('setting');await tick();
 check(!panel.hidden && !canvas.hidden,'page preview visible');
 check(!document.getElementById('setting-pdf-width-mm').disabled,'custom dimensions enabled');
 const pixel=(x,y)=>Array.from(canvas.getContext('2d').getImageData(x,y,1,1).data).slice(0,3).join(',');
 check(pixel(120,120)==='255,0,0','first input is page one');
 check(pixel(120,30)==='255,255,255','fit retains whitespace for wide first image');
 set('fit','crop');updatePDFLayout('setting');await tick();
 check(pixel(120,30)==='255,0,0','crop fills content rectangle');check(pixel(120,10)==='255,255,255','crop still respects margin');
 state.files.reverse();updatePDFLayout('setting');await tick();check(pixel(120,120)==='0,0,255','preview follows reordered inputs');
 set('page-size','letter');set('orientation','landscape');set('margin-mm','0');updatePDFLayout('setting');
 const layout=pdfLayoutValues('setting');check(layout.width===1584 && layout.height===1224 && layout.margin===0,'Letter landscape geometry matches backend at 144 dpi');
 const data=new FormData();appendPDFLayout(data,'setting');check(data.get('pdf_page_size')==='letter' && data.get('pdf_margin_mm')==='0','request includes zero margin');check(!data.has('pdf_width_mm'),'irrelevant custom fields omitted');
 set('page-size','custom');set('width-mm','601');updatePDFLayout('setting');check(!validatePDFLayout('setting'),'oversized custom side blocks request');
 set('width-mm','100');set('margin-mm','50');updatePDFLayout('setting');check(!validatePDFLayout('setting'),'margin cannot consume page');
 set('page-size','original');updatePDFLayout('setting');const original=new FormData();appendPDFLayout(original,'setting');check([...original.keys()].join(',')==='pdf_page_size','original ignores disabled stale dimensions');
 format.value='gif';updatePDFLayout('setting');const other=new FormData();appendPDFLayout(other,'setting');check([...other].length===0,'non-PDF request contains no page fields');
 check(document.getElementById('setting-pdf-width-mm').value==='100','switching formats preserves draft');
 check(snapshotSettings()['setting-pdf-page-size']==='original' && snapshotSettings()['setting-pdf-fit']==='crop','settings snapshot retains page controls');
 }catch(error){failures.push(error.stack||error.message);}
 const result=document.createElement('pre');result.id='pdf-layout-result';result.textContent=JSON.stringify(failures);document.body.append(result);})();`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'').replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+script+'</script></body>');
 const file=join(work,'fixture.html');writeFileSync(file,fixture);
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--virtual-time-budget=3000','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:10*1024*1024});
 assert.equal(run.status,0,run.stderr);const result=run.stdout.match(/<pre id="pdf-layout-result">([^<]+)<\/pre>/);assert.ok(result,'browser completed');assert.deepEqual(JSON.parse(result[1]),[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});
