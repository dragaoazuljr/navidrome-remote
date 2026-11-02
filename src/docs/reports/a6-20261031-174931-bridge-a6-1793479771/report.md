# A6 — Evidence: browser-level matrix (bridge.js)

- host: `dev-workspace`, chrome: `/usr/bin/google-chrome`
- rig: service `http://127.0.0.1:43131`, upstream `127.0.0.1:40975`, sink `/tmp/bridge-a6-1793479771/mock-ipc.sock`
- asset sha256: `715075de57418bb93c4482cfd0b0e5375a1375a4a9a963e128dbd862a825b004`

| caso | veredito | detalhes |
|---|---|---|
| T18 | ❌ | race UI x external on the same resource inside the window: last-write-wins, LOGGED (plane_conflict) and COUNTED on /status (A8)<br>{✓} bridge engaged — engaged<br>{✓} external write accepted (engine volume -> 80, last-write-wins by arrival order) — 200 {'ok': True, 'level': 80}<br>{✗} engine holds the LAST write (80) — 80<br>{✗} spy LOGGED the race (plane_conflict, winner=ext, loser=ui, resource=volume) — none<br>{✗} /status COUNTS the accumulated conflicts ('conflicts' present and >= 1) — 0<br>{✓} UI volume slider followed the WINNING value (80) — 80 |

_Credenciais sintéticas derivadas em página; este documento passa pelo redator do espião antes da persistência._
