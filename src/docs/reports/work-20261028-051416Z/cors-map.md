## Mapa CORS/OPTIONS por rota — v0.64.2 (cláusula A)

> Comparação direta (upstream) vs proxy (nós) para a mesma célula (rota, contexto, origin, método).
> `espelhado=NO` indica **divergência de protocolo** (potencial vermelho). Detalhes no JSON.

| rota | ctx | origin | acr | status D→P | espelhado | diffs |
|------|-----|--------|-----|-----------|-----------|-------|
| `/` | anon | <none> | GET | 302→302 | YES | - |
| `/` | anon | <none> | GET | 405→405 | YES | - |
| `/` | anon | <none> | POST | 405→405 | YES | - |
| `/` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/` | session | <none> | GET | 302→302 | YES | - |
| `/` | session | <none> | GET | 405→405 | YES | - |
| `/` | session | <none> | POST | 405→405 | YES | - |
| `/` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/api/v1/health` | anon | <none> | GET | 404→200 | **NO** | status 404!=200; vary: ['Origin'] != None |
| `/api/v1/health` | anon | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/health` | anon | <none> | POST | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | GET | 404→200 | **NO** | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | anon | http://127.0.0.1:8481 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | GET | 404→200 | **NO** | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | anon | http://192.168.0.50:5173 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | session | <none> | GET | 404→200 | **NO** | status 404!=200; vary: ['Origin'] != None |
| `/api/v1/health` | session | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/health` | session | <none> | POST | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | GET | 404→200 | **NO** | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | session | http://127.0.0.1:8481 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | GET | 404→200 | **NO** | status 404!=200; access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/health` | session | http://192.168.0.50:5173 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | anon | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | anon | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | anon | <none> | POST | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | GET | 404→404 | **NO** | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | anon | http://127.0.0.1:8481 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | GET | 404→404 | **NO** | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | anon | http://192.168.0.50:5173 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | session | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | session | <none> | GET | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | session | <none> | POST | 404→404 | **NO** | vary: ['Origin'] != None |
| `/api/v1/play` | session | http://127.0.0.1:8481 | GET | 404→404 | **NO** | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | session | http://127.0.0.1:8481 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | session | http://127.0.0.1:8481 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | session | http://192.168.0.50:5173 | GET | 404→404 | **NO** | access-control-allow-origin: ['*'] != None; vary: ['Origin'] != None |
| `/api/v1/play` | session | http://192.168.0.50:5173 | GET | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['GET'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/api/v1/play` | session | http://192.168.0.50:5173 | POST | 200→404 | **NO** | status 200!=404; access-control-allow-origin: ['*'] != None; access-control-allow-methods: ['POST'] != None; access-control-allow-headers: ['X-Navidrome-Test'] != None |
| `/app/` | anon | <none> | GET | 200→200 | YES | - |
| `/app/` | anon | <none> | GET | 200→200 | YES | - |
| `/app/` | anon | <none> | POST | 200→200 | YES | - |
| `/app/` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/app/` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/app/` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/app/` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/app/` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/app/` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/app/` | session | <none> | GET | 200→200 | YES | - |
| `/app/` | session | <none> | GET | 200→200 | YES | - |
| `/app/` | session | <none> | POST | 200→200 | YES | - |
| `/app/` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/app/` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/app/` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/app/` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/app/` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/app/` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/cover-art` | anon | <none> | GET | 302→302 | YES | - |
| `/cover-art` | anon | <none> | GET | 405→405 | YES | - |
| `/cover-art` | anon | <none> | POST | 405→405 | YES | - |
| `/cover-art` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/cover-art` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/cover-art` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/cover-art` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/cover-art` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/cover-art` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/cover-art` | session | <none> | GET | 302→302 | YES | - |
| `/cover-art` | session | <none> | GET | 405→405 | YES | - |
| `/cover-art` | session | <none> | POST | 405→405 | YES | - |
| `/cover-art` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/cover-art` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/cover-art` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/cover-art` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/cover-art` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/cover-art` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/favicon.ico` | anon | <none> | GET | 302→302 | YES | - |
| `/favicon.ico` | anon | <none> | GET | 405→405 | YES | - |
| `/favicon.ico` | anon | <none> | POST | 405→405 | YES | - |
| `/favicon.ico` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/favicon.ico` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/favicon.ico` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/favicon.ico` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/favicon.ico` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/favicon.ico` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/favicon.ico` | session | <none> | GET | 302→302 | YES | - |
| `/favicon.ico` | session | <none> | GET | 405→405 | YES | - |
| `/favicon.ico` | session | <none> | POST | 405→405 | YES | - |
| `/favicon.ico` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/favicon.ico` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/favicon.ico` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/favicon.ico` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/favicon.ico` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/favicon.ico` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/playlists` | anon | <none> | GET | 302→302 | YES | - |
| `/playlists` | anon | <none> | GET | 405→405 | YES | - |
| `/playlists` | anon | <none> | POST | 405→405 | YES | - |
| `/playlists` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/playlists` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/playlists` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/playlists` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/playlists` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/playlists` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/playlists` | session | <none> | GET | 302→302 | YES | - |
| `/playlists` | session | <none> | GET | 405→405 | YES | - |
| `/playlists` | session | <none> | POST | 405→405 | YES | - |
| `/playlists` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/playlists` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/playlists` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/playlists` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/playlists` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/playlists` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/queue` | anon | <none> | GET | 302→302 | YES | - |
| `/queue` | anon | <none> | GET | 405→405 | YES | - |
| `/queue` | anon | <none> | POST | 405→405 | YES | - |
| `/queue` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/queue` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/queue` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/queue` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/queue` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/queue` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/queue` | session | <none> | GET | 302→302 | YES | - |
| `/queue` | session | <none> | GET | 405→405 | YES | - |
| `/queue` | session | <none> | POST | 405→405 | YES | - |
| `/queue` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/queue` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/queue` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/queue` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/queue` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/queue` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/rest/about` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/about` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/about` | anon | <none> | POST | 404→404 | YES | - |
| `/rest/about` | anon | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/about` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/about` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/about` | anon | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/about` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/about` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/about` | session | <none> | GET | 404→404 | YES | - |
| `/rest/about` | session | <none> | GET | 404→404 | YES | - |
| `/rest/about` | session | <none> | POST | 404→404 | YES | - |
| `/rest/about` | session | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/about` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/about` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/about` | session | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/about` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/about` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/artist` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/artist` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/artist` | anon | <none> | POST | 404→404 | YES | - |
| `/rest/artist` | anon | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/artist` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/artist` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/artist` | anon | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/artist` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/artist` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/artist` | session | <none> | GET | 404→404 | YES | - |
| `/rest/artist` | session | <none> | GET | 404→404 | YES | - |
| `/rest/artist` | session | <none> | POST | 404→404 | YES | - |
| `/rest/artist` | session | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/artist` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/artist` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/artist` | session | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/artist` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/artist` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/ping` | anon | <none> | GET | 200→200 | YES | - |
| `/rest/ping` | anon | <none> | GET | 200→200 | YES | - |
| `/rest/ping` | anon | <none> | POST | 200→200 | YES | - |
| `/rest/ping` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/ping` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/ping` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/ping` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/ping` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/ping` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/ping` | session | <none> | GET | 200→200 | YES | - |
| `/rest/ping` | session | <none> | GET | 200→200 | YES | - |
| `/rest/ping` | session | <none> | POST | 200→200 | YES | - |
| `/rest/ping` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/ping` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/ping` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/ping` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/ping` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/ping` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/song` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/song` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/song` | anon | <none> | POST | 404→404 | YES | - |
| `/rest/song` | anon | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/song` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/song` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/song` | anon | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/song` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/song` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/song` | session | <none> | GET | 404→404 | YES | - |
| `/rest/song` | session | <none> | GET | 404→404 | YES | - |
| `/rest/song` | session | <none> | POST | 404→404 | YES | - |
| `/rest/song` | session | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/song` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/song` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/song` | session | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/song` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/song` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/user` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/user` | anon | <none> | GET | 404→404 | YES | - |
| `/rest/user` | anon | <none> | POST | 404→404 | YES | - |
| `/rest/user` | anon | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/user` | anon | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/user` | anon | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/user` | anon | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/user` | anon | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/user` | anon | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/rest/user` | session | <none> | GET | 404→404 | YES | - |
| `/rest/user` | session | <none> | GET | 404→404 | YES | - |
| `/rest/user` | session | <none> | POST | 404→404 | YES | - |
| `/rest/user` | session | http://127.0.0.1:8481 | GET | 404→404 | YES | - |
| `/rest/user` | session | http://127.0.0.1:8481 | GET | 200→200 | YES | - |
| `/rest/user` | session | http://127.0.0.1:8481 | POST | 200→200 | YES | - |
| `/rest/user` | session | http://192.168.0.50:5173 | GET | 404→404 | YES | - |
| `/rest/user` | session | http://192.168.0.50:5173 | GET | 200→200 | YES | - |
| `/rest/user` | session | http://192.168.0.50:5173 | POST | 200→200 | YES | - |
| `/stream` | anon | <none> | GET | 302→302 | YES | - |
| `/stream` | anon | <none> | GET | 405→405 | YES | - |
| `/stream` | anon | <none> | POST | 405→405 | YES | - |
| `/stream` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/stream` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/stream` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/stream` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/stream` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/stream` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/stream` | session | <none> | GET | 302→302 | YES | - |
| `/stream` | session | <none> | GET | 405→405 | YES | - |
| `/stream` | session | <none> | POST | 405→405 | YES | - |
| `/stream` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/stream` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/stream` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/stream` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/stream` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/stream` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/version` | anon | <none> | GET | 302→302 | YES | - |
| `/version` | anon | <none> | GET | 405→405 | YES | - |
| `/version` | anon | <none> | POST | 405→405 | YES | - |
| `/version` | anon | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/version` | anon | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/version` | anon | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/version` | anon | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/version` | anon | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/version` | anon | http://192.168.0.50:5173 | POST | 405→405 | YES | - |
| `/version` | session | <none> | GET | 302→302 | YES | - |
| `/version` | session | <none> | GET | 405→405 | YES | - |
| `/version` | session | <none> | POST | 405→405 | YES | - |
| `/version` | session | http://127.0.0.1:8481 | GET | 302→302 | YES | - |
| `/version` | session | http://127.0.0.1:8481 | GET | 405→405 | YES | - |
| `/version` | session | http://127.0.0.1:8481 | POST | 405→405 | YES | - |
| `/version` | session | http://192.168.0.50:5173 | GET | 302→302 | YES | - |
| `/version` | session | http://192.168.0.50:5173 | GET | 405→405 | YES | - |
| `/version` | session | http://192.168.0.50:5173 | POST | 405→405 | YES | - |

**Totais:** 270 pares medidos; 234 espelhados; 36 com divergência.

