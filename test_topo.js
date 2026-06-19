const fs = require('fs');
const jsdom = require('jsdom');
const { JSDOM } = jsdom;

let html = fs.readFileSync('ui/templates/topology.html', 'utf8')
                .replace("{{ all_links_json | safe }}", fs.readFileSync('topology.json', 'utf8').split('"links":')[1].split(',"port_channels"')[0])
                .replace("{{ all_devices_json | safe }}", fs.readFileSync('topology.json', 'utf8').split('"devices":')[1].split(',"links"')[0])
                .replace("{{ all_port_channels_json | safe }}", '[]')
                .replace("{{ ifidx_to_descr_json | safe }}", '{}')
                .replace("{{ positions | tojson if positions else '{}' }}", '{}')
                .replace("const socket = io();", "const socket = {on: ()=>{}};");

const dom = new JSDOM(html, {
    runScripts: "dangerously",
    resources: "usable"
});

setTimeout(() => {
    try {
        dom.window.buildNetwork('all');
        console.log("EdgesDS Length:", dom.window.edgesDS.get().length);
    } catch (err) {
        console.error("Crash:", err);
    }
    process.exit(0);
}, 2000);
