import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
test('dialog focus boundary, restoration, names and keyboard controls in Chrome',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-accessibility-'));
 try{
 const start=html.indexOf('let activeModal =');
 const helper=start<0?'':html.slice(start,html.indexOf('const reconvertModal =',start));
 const presetStart=html.indexOf('function renderPresetRow(');
 const preset=html.slice(presetStart,html.indexOf('\n}',presetStart)+2);
 const disclosures=['', 'reconvert-'].map(prefix=>{const a=html.indexOf("document.getElementById('"+prefix+"advanced-toggle-btn').addEventListener");return html.slice(a,html.indexOf('\n});',a)+4);}).join('\n');
 const script=`const failures=[];const check=(ok,msg)=>{if(!ok)failures.push(msg);};
 try {
 const opener=document.getElementById('nav-dashboard');opener.focus();
 for(const id of ['reconvert-modal','lightbox']){
  const root=document.getElementById(id);root.style.display='flex';
  check(root.getAttribute('role')==='dialog' && root.getAttribute('aria-modal')==='true',id+' semantics');
  check(root.getAttribute('aria-label') || root.querySelector('#'+root.getAttribute('aria-labelledby'))?.textContent.trim(),id+' name');
  const close=()=>{endModal(root);root.style.display='none';};
  beginModal(root,close);
  check(root.contains(document.activeElement),id+' initial focus');
  check(document.querySelector('main').inert,id+' inert background');
  check(!document.getElementById('toast-container').inert,id+' announcements remain available');
  opener.focus();check(root.contains(document.activeElement),id+' rejects background focus');
  const items=modalFocusables(root);items.at(-1).focus();
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}));
  check(document.activeElement===items[0],id+' Tab wraps');
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true,cancelable:true}));
  check(document.activeElement===items.at(-1),id+' Shift Tab wraps');
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  check(root.style.display==='none' && !document.querySelector('main').inert,id+' Escape closes');
  check(document.activeElement===opener,id+' restores opener');
 }
 check(matchMedia('(prefers-reduced-motion: reduce)').matches,'reduced motion preference');
 check(getComputedStyle(document.getElementById('header-converting-spinner')).animationName==='none','reduced motion stops spinner animation');
 const removed=document.createElement('button');document.body.append(removed);removed.focus();
 const root=document.getElementById('reconvert-modal');root.style.display='flex';beginModal(root,()=>{});removed.remove();endModal(root);root.style.display='none';
 check(document.activeElement===opener,'removed opener uses navigation fallback');
 const PRESETS={},state={userPresets:{Example:{}}},escapeHtml=s=>s;
 ${preset}
 renderPresetRow();const del=document.querySelector('[data-del-preset]');check(del.tagName==='BUTTON' && del.tabIndex===0,'preset delete keyboard button');
 for(const prefix of ['', 'reconvert-']){const b=document.getElementById(prefix+'advanced-toggle-btn');check(b.getAttribute('aria-expanded')==='false' && document.getElementById(b.getAttribute('aria-controls')),prefix+' disclosure state');b.click();check(b.getAttribute('aria-expanded')==='true' && document.getElementById(b.getAttribute('aria-controls')).style.display==='block',prefix+' opens disclosure');b.click();check(b.getAttribute('aria-expanded')==='false',prefix+' closes disclosure');}
 check(!document.querySelector('[role=tab]'),'mode buttons do not pretend to implement incomplete tabs');
 for(const button of document.querySelectorAll('button')){
  const clone=button.cloneNode(true);clone.querySelectorAll('[aria-hidden=true]').forEach(n=>n.remove());
  check(button.getAttribute('aria-label') || button.title || clone.textContent.trim(),'unnamed button '+button.id);
 }
 }catch(e){failures.push(e.message);}
 const result=document.createElement('pre');result.id='a11y-result';result.textContent=JSON.stringify(failures);document.body.append(result);`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'').replace('</body>','<script>'+helper+disclosures+script+'</script></body>');
 const file=join(work,'fixture.html');writeFileSync(file,fixture);
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--force-prefers-reduced-motion','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:8*1024*1024});
 assert.equal(run.status,0,run.stderr);const match=run.stdout.match(/<pre id="a11y-result">([^<]+)<\/pre>/);assert.ok(match);assert.deepEqual(JSON.parse(match[1]),[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});
test('keyboard focus ring contrast against the shell exceeds 3:1',()=>{
 const rgb=hex=>hex.match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);
 const luminance=hex=>rgb(hex).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
 const focus=html.match(/:focus-visible\s*\{[^}]*outline:\s*2px solid #([0-9a-f]{6})/i)?.[1];
 const background=html.match(/body\s*\{\s*background-color:\s*#([0-9a-f]{6})/i)?.[1];
 assert.ok(focus && background);
 const a=luminance(focus),b=luminance(background),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);
 assert.ok(ratio>=3);console.log('Focus ring/shell contrast: '+ratio.toFixed(2)+':1');
});
