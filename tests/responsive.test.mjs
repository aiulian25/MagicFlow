import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
const css=readFileSync(new URL('../app/static/vendor/tailwind.css',import.meta.url),'utf8');
for(const width of [320,390,768,640,1280]) test('layout fits '+width+' CSS pixels',()=>{
 const work=mkdtempSync(join(tmpdir(),'magicflow-responsive-'));
 try{
 const script=`const failures=[];const check=(ok,msg)=>{if(!ok)failures.push(msg);};
 const fits=(node,label)=>{const r=node.getBoundingClientRect();check(r.left>=-1&&r.right<=innerWidth+1,label+' '+r.left+'..'+r.right);};
 for(const view of ['dashboard','files','settings']){
  for(const name of ['dashboard','files','settings'])document.getElementById('view-'+name).hidden=name!==view;
  if(view==='dashboard')for(const mode of ['settings-section','extract-section','convert-section']){
   for(const id of ['settings-section','extract-section','convert-section'])document.getElementById(id).hidden=id!==mode;
   document.querySelectorAll('#'+mode+' input,#'+mode+' select,#'+mode+' button').forEach(n=>{if(n.getClientRects().length)fits(n,mode+' '+n.id);});
   check(document.documentElement.scrollWidth<=innerWidth+1,mode+' page overflow '+document.documentElement.scrollWidth);
  }
  fits(document.querySelector('main'),view+' main');
  check(document.querySelector('main').getBoundingClientRect().width>=Math.min(innerWidth-32,600),view+' usable width');
  check(document.documentElement.scrollWidth<=innerWidth+1,view+' overflow');
 }
 for(const n of document.querySelectorAll('[role=tab],aside a'))fits(n,'navigation '+n.id);
 const fab=document.getElementById('convert-fab');fab.style.display='block';fits(fab,'primary action');
 document.getElementById('reconvert-modal').style.display='flex';
 document.querySelectorAll('#reconvert-modal input,#reconvert-modal select,#reconvert-submit').forEach(n=>{if(n.getClientRects().length)fits(n,'modal '+n.id);});
 document.querySelectorAll('body *').forEach(n=>{if(n.getClientRects().length && n.getBoundingClientRect().right>innerWidth+1 && !n.closest('#reconvert-modal')) failures.push('outside '+n.tagName+'#'+n.id+'.'+n.className+' '+n.getBoundingClientRect().right);});
 const result=document.createElement('pre');result.id='responsive-result';result.textContent=JSON.stringify({width:innerWidth,failures});document.body.append(result);`;
 const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'').replace(/<link\b[^>]*>/g,'')
 .replace('</head>','<style>'+css+'</style><link rel="stylesheet" href="file://'+resolve('app/static/vendor/fonts.css')+'"></head>')
 .replace('</body>','<script>'+script+'</script></body>');
 const file=join(work,'fixture.html');const escaped=fixture.replaceAll('&','&amp;').replaceAll('\"','&quot;');
 writeFileSync(file,'<iframe style="border:0;width:'+width+'px;height:900px" srcdoc="'+escaped+'"></iframe><script>window.onload=()=>{document.body.append(document.querySelector("iframe").contentDocument.getElementById("responsive-result").cloneNode(true));};</script>');
 const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',['--headless','--no-sandbox','--disable-gpu','--disable-background-networking','--no-first-run','--force-device-scale-factor=1','--window-size='+width+',900','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file],{encoding:'utf8',timeout:30000,maxBuffer:8*1024*1024});
 assert.equal(run.status,0,run.stderr);const match=run.stdout.match(/<pre id="responsive-result">([^<]+)<\/pre>/);assert.ok(match);const result=JSON.parse(match[1]);
 assert.equal(result.width,width);assert.deepEqual(result.failures,[]);
 }finally{rmSync(work,{recursive:true,force:true});}
});
