import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(process.env.MAGICFLOW_UI_SOURCE || new URL('../app/static/index.html',import.meta.url),'utf8');
function load(names, extras={}) {
    const elements=new Map();
    const element=id=>{if(!elements.has(id)) elements.set(id,{value:'',checked:false,style:{},classList:{toggle(){}},setAttribute(){}});return elements.get(id);};
    const context=vm.createContext({state:{mode:'combine',files:[],convertFiles:[],extractFile:null},document:{getElementById:element},showToast(){},showStatus(){},syncUI(){},renderConvertCards(){},setHeaderConverting(){},hideDownload(){},hideStatus(){},clearRejected(){},loadRecentConversions(){},readNumericField:()=>90,FormData:class {constructor(){this.values={};} append(k,v){this.values[k]=v;}},...extras});
    for(const name of names){const start=html.indexOf(`function ${name}(`);assert.ok(start>=0,name);vm.runInContext(html.slice(html.lastIndexOf('async ',start)===start-6?start-6:start,html.indexOf('\n}',start)+2),context);}
    return {context,element};
}
test('navigation preserves every draft, progress and results; active tab is a no-op',()=>{
    let clears=0,renders=0,hides=0;
    const modes=Object.fromEntries(['combine','convert','extract'].map(n=>[n,{btn:n,drop:{},clear(){clears++;},render(){renders++;}}]));
    const {context:c}=load(['setMode'],{MODES:modes,hideDownload(){hides++;},hideStatus(){hides++;}});
    const drafts=[{},{}];c.state.files=drafts;c.state.converting=true;
    c.setMode('convert');c.setMode('combine');const count=renders;c.setMode('combine');
    assert.equal(clears,0);assert.equal(hides,0);assert.equal(c.state.files,drafts);assert.equal(renders,count);
});
test('running operations guard destructive draft changes and concurrent imports',async()=>{
    const names=['clearCombineFiles','clearConvertFiles','clearExtractFile','clearAll','handleCombineFiles','handleConvertFiles','handleExtractFile','removeFile','moveLeft','moveRight','removeExtractFile','openReconvert','closeReconvert','editInCombine'];
    const {context:c}=load(names);c.state.converting=true;
    for(const n of names) await c[n]([]);
});
test('batch membership, source files and settings survive changes during a request; successes are skipped',async()=>{
    let resume;const gate=new Promise(r=>resume=r);const requests=[];
    const {context:c,element}=load(['startConvertAll'],{fetch:async(_url,options)=>{requests.push(options.body.values);if(requests.length===1)await gate;return {ok:true,json:async()=>({output_filename:'out.png'})};}});
    element('convert-format').value='png';
    const a={file:{name:'a.png'}},b={file:{name:'b.png'}},already={file:{name:'done.png'},status:'done'};
    c.state.convertFiles=[a,b,already];const pending=c.startConvertAll();
    c.state.convertFiles.push({file:{name:'extra.png'}});b.file={name:'changed.png'};element('convert-format').value='jpg';
    resume();await pending;
    assert.deepEqual(requests.map(r=>r.file.name),['a.png','b.png']);assert.deepEqual(requests.map(r=>r.out_format),['png','png']);
    assert.equal(a.status,'done');assert.equal(b.status,'done');assert.equal(c.state.converting,false);
});
test('failed edit preserves the prior draft and releases the busy guard',async()=>{
    const {context:c}=load(['editInCombine'],{EXPLODE_EXTS:new Set(['.gif']),fetch:async()=>{throw new Error('offline');}});
    const original=[{file:{name:'work.png'}}];c.state.files=original;
    const pending=c.editInCombine({name:'source.gif'});assert.equal(c.state.converting,true);await pending;
    assert.equal(c.state.files,original);assert.equal(c.state.converting,false);
});
test('mixed-success retry submits only the failed item and keeps successful results',async()=>{
    const requests=[];let fail=true;
    const {context:c}=load(['startConvertAll'],{fetch:async(_url,options)=>{const name=options.body.values.file.name;requests.push(name);return {ok:!(fail&&name==='a.png'),json:async()=>({detail:'failed',output_filename:name+'.out'})};}});
    const a={file:{name:'a.png'}},b={file:{name:'b.png'}};c.state.convertFiles=[a,b];
    await c.startConvertAll();assert.equal(a.status,'error');assert.equal(b.status,'done');
    fail=false;await c.startConvertAll();
    assert.deepEqual(requests,['a.png','b.png','a.png']);assert.equal(a.status,'done');assert.equal(b.resultName,'b.png.out');
});
