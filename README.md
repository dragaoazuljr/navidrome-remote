# Navidrome Remote

Player remoto de música. Um Orange Pi Zero 2W, sempre ligado, serve a interface
oficial do Navidrome e toca no próprio SBC. Qualquer navegador da rede escolhe a
faixa e manda play, pause, seek e fila. Fechar a aba não para a música.

O áudio sai por um DAC USB-C na entrada auxiliar do Harman Kardon Go+ Play 3.
Bluetooth fica como reserva.

O servidor de biblioteca já existe: Navidrome v0.64.2 no LXC 9102
(`192.168.0.200:4533`). Este repositório é o serviço do SBC: proxy transparente
da interface, `bridge.js` e a API `/api/v1/*`. O `mpv` toca o arquivo original,
sem reencode.

Ainda não há código. O spike de áudio espera o Orange Pi e o DAC.
