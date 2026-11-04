import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
function setup(fetch){
 const messages=[];let quality='70';const source=html.slice(html.indexOf('let _settingsSaveTimer = null'),html.indexOf('// SERVER CONFIG',html.indexOf('let _settingsSaveTimer = null'))).replace(/\/\/ ═+\s*$/,'');
 const context=vm.createContext({fetch,clearTimeout(){},setTimeout(){},showToast:m=>messages.push(m),confirm:()=>false,snapshotSettings:()=>({'setting-quality':quality,user_presets:'{}'}),MAX_USER_PRESETS:10,PERSIST_IDS:['setting-quality','setting-flip'],state:{userPresets:{}},document:{getElementById:id=>id==='setting-quality'?{type:'number'}:id==='setting-flip'?{type:'checkbox'}:null},applySettings(){},renderPresetRow(){},updateResizeHint(){},updateDelayHint(){},updateTimingPreview(){},updateSizeEstimate(){}});
 vm.runInContext(source,context);vm.runInContext("_settingsRevision='r0';",context);
 return {c:context,messages,quality:value=>quality=value,run:code=>vm.runInContext(code,context)};
}
test('saves serialize and changes during a delayed response use its new revision',async()=>{
 let resume;const gate=new Promise(r=>resume=r);const requests=[];let active=0,max=0;
 const x=setup(async(_url,opts)=>{active++;max=Math.max(max,active);requests.push(opts);if(requests.length===1)await gate;active--;return {ok:true,json:async()=>({revision:'r'+requests.length})};});
 x.c.persistSettings();const pending=x.c.flushSettings();x.quality('90');x.c.persistSettings();await x.c.flushSettings();resume();await pending;
 assert.equal(max,1);assert.equal(requests.length,2);assert.equal(requests[1].headers['If-Match'],'r1');assert.equal(JSON.parse(requests[1].body)['setting-quality'],'90');
});
test('conflict keeps local draft without an overwrite or saved-success message',async()=>{
 let calls=0;const x=setup(async()=>{calls++;return {ok:false,status:409};});x.c.state.userPresets={local:{}};
 x.c.persistSettings();await x.c.flushSettings();assert.equal(calls,1);assert.ok(x.c.state.userPresets.local);assert.ok(!x.messages.includes('Settings saved'));
});
test('offline save remains dirty and cannot report durable success',async()=>{
 const x=setup(async()=>{throw new Error('offline');});x.c.persistSettings();await x.c.flushSettings();assert.equal(x.run('_settingsDirty'),true);assert.ok(!x.messages.includes('Settings saved'));
});
test('loaded presets reject unknown IDs and wrong checkbox types before applying',()=>{
 const x=setup();for(const value of [{x:{unknown:1}},{x:{'setting-flip':'false'}},[]])assert.throws(()=>x.c.validatedPresets(JSON.stringify(value)));
 assert.equal(x.c.validatedPresets('{"x":{"setting-quality":"80","setting-flip":false}}').x['setting-flip'],false);
});
test('conflict reload does not overwrite edits made while the newer document loads',async()=>{
 let resume,loading;const started=new Promise(r=>loading=r);const gate=new Promise(r=>resume=r);let calls=0,applied=0;
 const x=setup(async()=>{if(++calls===1)return {ok:false,status:409};loading();await gate;return {ok:true,json:async()=>({revision:'new',settings:{user_presets:'{}'}})};});
 x.c.confirm=()=>true;x.c.applySettings=()=>applied++;
 x.c.persistSettings();const pending=x.c.flushSettings();await started;x.quality('95');x.c.persistSettings();resume();await pending;
 assert.equal(applied,0);assert.equal(x.run('_settingsDirty'),true);
});
test('accepting conflict reload hydrates the newer presets and revision',async()=>{
 let calls=0;const x=setup(async()=>++calls===1?{ok:false,status:409}:{ok:true,json:async()=>({revision:'new',settings:{user_presets:'{"shared":{"setting-quality":"85"}}'}})});
 x.c.confirm=()=>true;x.c.persistSettings();await x.c.flushSettings();assert.ok(x.c.state.userPresets.shared);assert.equal(x.run('_settingsRevision'),'new');
});
