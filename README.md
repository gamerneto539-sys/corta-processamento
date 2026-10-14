# corta-processamento

Servidor do Corta para analisar vídeos públicos e preparar MP4 ou MP3 com
corte por minutagem. O site chama esta API; o visitante não precisa instalar
o programa.

## Render

Use o Dockerfile na raiz, plano gratuito e a variável protegida
CORTA_BACKEND_TOKEN com pelo menos 32 caracteres aleatórios. O serviço escuta
em 0.0.0.0:$PORT. /healthz é a verificação pública de saúde. A API exige
Authorization: Bearer e X-Corta-Owner com 64 caracteres hexadecimais.

No Site, configure CORTA_BACKEND_URL com a URL HTTPS e CORTA_BACKEND_TOKEN com
o mesmo segredo. O segredo fica nas variáveis protegidas, fora do código.

Limite padrão: 256 MiB por mídia, duas tarefas simultâneas e resultados
temporários por uma hora. O plano gratuito pode suspender por inatividade.
Reinícios removem dados temporários.

O acesso a YouTube, Instagram, TikTok e Pinterest depende das plataformas.
Conteúdos privados, login obrigatório, DRM e lives não são processados.

## Verificação local

Instale cloud/requirements.txt, FFmpeg e Deno e execute:

    python -m unittest discover -s tests -v

Os testes cobrem autenticação, isolamento, expiração, validação de links,
codecs e cortes com ferramentas reais. O download HTTP dos testes usa mídia
controlada localmente.
