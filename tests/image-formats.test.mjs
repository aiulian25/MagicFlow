/** FEAT-007 image-only input/output controls and saved capability choices. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
const css=readFileSync(process.env.MAGICFLOW_CSS_SOURCE || new URL('../app/static/vendor/tailwind.css',import.meta.url),'utf8');

test('image formats remain capability-driven without rewriting unavailable saved choices',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-image-formats-'));
 try{
 const fn=name=>{const start=html.indexOf('function '+name+'(');return html.slice(start,html.indexOf('\n}',start)+2);};
 const capStart=html.indexOf('const FORMAT_SELECTS = [');
 const capCode=html.slice(capStart,html.indexOf('\nasync function loadConfig',capStart));
 const numeric=html.slice(html.indexOf('const NUMERIC_FIELDS ='),html.indexOf('function blockInvalidFields('));
 const start=html.indexOf('async function startConvertAll('),convert=html.slice(start,html.indexOf('\n}',start)+2);
 const script=`
 const failures=[],check=(ok,message)=>{if(!ok)failures.push(message);};
 const state={convertFiles:[],converting:false,userPresets:{Keep:{'setting-format':'gif'}}};
 let CONFIG={still_output_formats:['png','avif'],animation_output_formats:[],explode_output_formats:['png'],unavailable:{'still:ico':'ICO denied by policy'}};
 let ALLOWED_EXTS=new Set(['.avif']),RAW_EXTS=new Set(),ALLOWED_TYPES=new Set(['image/avif']),requests=[];
 const showStatus=(...args)=>requests.push(['status',args]),showToast=()=>{},formatBytes=n=>String(n),
 renderConvertCards=()=>{},setHeaderConverting=()=>{},hideDownload=()=>{},loadRecentConversions=()=>{};
 const convertFormat=document.getElementById('convert-format'),STILL_LOSSY=new Set(['jpg','webp','avif','heic']);
 ${fn('unavailableInputReason')}
 ${fn('typeRejection')}
 ${numeric}
 ${fn('updateConvertFormatUI')}
 ${capCode}
 ${convert}
 const image=new File(['fixture'],'input.avif',{type:'image/avif'});
 window.fetch=async(url,options)=>{requests.push([url,Object.fromEntries(options.body)]);return {ok:true,json:async()=>({output_filename:'icon.ico',file_size:100,delete_after_download:false})};};
 (async()=>{try{
 check(typeRejection(image)===null,'AVIF MIME and extension accepted');
 check(typeRejection(new File(['x'],'input.avif',{type:'text/html'}))!==null,'bad MIME rejected');
 const option=[...convertFormat.options].find(o=>o.value==='ico');
 check(option.parentElement.label==='More image formats','specialist option group');
 convertFormat.value='ico';document.getElementById('convert-icon-size').value='32';
 document.getElementById('convert-width').value='999';document.getElementById('convert-height').value='888';
 applyCapabilities();updateConvertFormatUI();
 check(convertFormat.value==='ico' && option.disabled && option.title.includes('ICO denied'),'saved unavailable format retained and explained');
 check(document.getElementById('convert-icon-size').value==='32','saved icon size retained');
 state.convertFiles=[{file:image,status:'ready'}];await startConvertAll();
 check(!requests.some(([url])=>url==='/convert-image'),'unavailable choice cannot submit');
 CONFIG.still_output_formats.push('ico');applyCapabilities();updateConvertFormatUI();
 check(!option.disabled && convertFormat.value==='ico','choice recovers when capability returns');
 check(document.getElementById('convert-width').disabled && document.getElementById('convert-height').disabled,'icon uses its dedicated dimensions');
 check(!document.getElementById('convert-icon-wrap').hidden && document.getElementById('convert-quality-wrap').style.display==='none','meaningful ICO controls');
 requests=[];await startConvertAll();
 const sent=requests.find(([url])=>url==='/convert-image')[1];
 check(sent.icon_size==='32' && !('width' in sent) && !('height' in sent),'single-square request ignores stale generic geometry');
 check(state.userPresets.Keep['setting-format']==='gif','named preset remains intact');
 convertFormat.value='png';updateConvertFormatUI();
 check(!document.getElementById('convert-width').disabled && document.getElementById('convert-width').value==='999','switching back preserves width draft');
 CONFIG.still_output_formats=[];applyCapabilities();
 check([...convertFormat.options].every(option=>option.disabled),'empty capability set disables all formats');
 }catch(error){failures.push(error.stack||error.message);}
 const result=document.createElement('pre');result.id='image-formats-result';result.textContent=JSON.stringify(failures);document.body.append(result);})();`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'')
  .replace('</head>','<style>'+css+'</style></head>').replace('</body>','<script>'+script+'</script></body>');
 const file=join(work,'fixture.html');writeFileSync(file,fixture);
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--virtual-time-budget=2000','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:10*1024*1024});
 assert.equal(run.status,0,run.stderr);const result=run.stdout.match(/<pre id="image-formats-result">([^<]+)<\/pre>/);assert.ok(result,'browser completed');assert.deepEqual(JSON.parse(result[1]),[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});
