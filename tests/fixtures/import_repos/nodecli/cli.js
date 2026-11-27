#!/usr/bin/env node
const [cmd, ...rest] = process.argv.slice(2);
if (cmd === "greet") console.log(`Hello, ${rest.join(" ")}!`);
else { console.error("usage: nodecli greet NAME"); process.exit(2); }
