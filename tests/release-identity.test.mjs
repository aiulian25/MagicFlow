/** FIX-025: Settings identifies the artifact returned by the server. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../app/static/index.html',import.meta.url),'utf8');
const from=html.indexOf('async function loadConfig()');
const source=html.slice(from,html.indexOf('    // Allow-lists and caps',from))+'\n}';
for(const build of [{version:'release-test',revision:'0123456789abcdef-dirty',source_sha256:'a'.repeat(64)},
                    {version:'development',revision:'working-tree',source_sha256:null}]) {
    test('Settings displays server build identity: '+build.version,async()=>{
        const nodes=new Map(['app-build-version','app-build-details'].map(id=>[id,{textContent:'Development build'}]));
        const context=vm.createContext({document:{getElementById:id=>nodes.get(id)},
            fetch:async()=>({ok:true,json:async()=>({build})})});
        vm.runInContext(source,context);await context.loadConfig();
        assert.equal(nodes.get('app-build-version').textContent,build.version);
        assert.match(nodes.get('app-build-details').textContent,/Revision/);
        assert.ok(nodes.get('app-build-details').title.includes(build.revision));
        if(build.source_sha256)assert.ok(nodes.get('app-build-details').title.includes(build.source_sha256));
        else assert.match(nodes.get('app-build-details').textContent,/unbuilt source/);
    });
}
test('unavailable config keeps the honest development fallback',async()=>{
    const context=vm.createContext({fetch:async()=>({ok:false})});
    vm.runInContext(source,context);await context.loadConfig();
    assert.doesNotMatch(html,/MagicFlow v1\.1\.1/);
    assert.match(html,/id="app-build-version"[^>]*>development</);
});
