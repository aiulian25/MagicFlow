// Real Chromium layout checks using the app's markup and committed CSS.
// CHROME_BIN can select another installed Chromium-compatible browser.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawnSync} from 'node:child_process';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
const css=readFileSync(process.env.MAGICFLOW_CSS_SOURCE || new URL('../app/static/vendor/tailwind.css',import.meta.url),'utf8');
test('semantic hidden wins against utilities and inline display, then restores layout',()=>{
    const work=mkdtempSync(join(tmpdir(),'magicflow-visibility-'));
    try {
        const script=`
        const failures=[];let checked=0;
        const check=(ok,msg)=>{if(!ok)failures.push(msg);};
        const hidden=[...document.querySelectorAll('[hidden]')];
        // Clone into the visible body so hidden ancestors cannot mask defects.
        for(const original of hidden){
            const node=original.cloneNode(true);node.removeAttribute('id');document.body.append(node);
            check(getComputedStyle(node).display==='none' && node.getClientRects().length===0,original.id+' hidden');
            node.hidden=false;
            check(getComputedStyle(node).display!=='none',original.id+' reveal');
            node.hidden=true;node.style.display='flex';
            check(node.getClientRects().length===0,original.id+' inline flex');
            node.hidden=false;check(getComputedStyle(node).display==='flex',original.id+' restore flex');
            node.remove();checked++;
        }
        const parent=document.createElement('div'),child=document.createElement('button');
        child.textContent='nested action';parent.append(child);document.body.append(parent);parent.hidden=true;
        check(child.getClientRects().length===0,'nested hidden view');parent.hidden=false;
        check(child.getClientRects().length>0,'nested restored view');
        const modal=document.createElement('div');document.body.append(modal);modal.style.display='none';
        check(modal.getClientRects().length===0,'closed modal');modal.style.display='flex';
        check(getComputedStyle(modal).display==='flex','open modal without hidden');
        const result=document.createElement('pre');result.id='visibility-result';
        result.textContent=JSON.stringify({checked,failures});document.body.append(result);
        `;
        const fixture=html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,'')
            .replace(/<link\b[^>]*>/g,'').replace('</head>','<style>'+css+'</style></head>')
            .replace('</body>','<script>'+script+'</script></body>');
        const file=join(work,'fixture.html');writeFileSync(file,fixture);
        const run=spawnSync(process.env.CHROME_BIN || '/usr/bin/google-chrome',[
            '--headless','--no-sandbox','--disable-gpu','--disable-background-networking',
            '--no-first-run','--user-data-dir='+join(work,'profile'),'--dump-dom','file://'+file
        ],{encoding:'utf8',timeout:30000,maxBuffer:8*1024*1024});
        assert.equal(run.status,0,run.stderr);
        const match=run.stdout.match(/<pre id="visibility-result">([^<]+)<\/pre>/);
        assert.ok(match,'browser must execute the fixture');const result=JSON.parse(match[1]);
        assert.ok(result.checked>=6,'exercise actual hidden controls');
        assert.deepEqual(result.failures,[]);console.log('Verified '+result.checked+' hidden elements and nested/modal transitions');
    } finally {rmSync(work,{recursive:true,force:true});}
});
