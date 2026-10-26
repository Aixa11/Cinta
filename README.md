# Cinta

Traductor de video y de documentos. Corre en tu computadora.

Subís un archivo o pegás un enlace. Cinta lo pasa al idioma que elijas. En un video podés pedir subtítulos, voz, o las dos. En un documento se traduce el texto y queda el formato: espacios, fuentes, imágenes y dibujos.

Lo dejo libre. Podés usarlo, copiarlo, modificarlo y compartirlo. La licencia está en [LICENSE](LICENSE).

## Cómo se usa

El inicio es el mismo para quien abra Cinta. Ahí se pide la traducción. Para ver videos, documentos, notas y grupos hay que entrar. Cada cuenta ve solo lo suyo. La cuenta es con correo y contraseña, o con Google.

La primera cuenta que se cree en esa computadora se queda con las traducciones que ya estaban guardadas ahí.

## Para levantarlo

Hace falta Node, Python 3.11, ffmpeg y conexión a internet. El archivo se procesa en la máquina. El texto y las voces salen por la red.

```bash
npm install
npm run build
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\python -m pip install -r pipeline\requirements.txt
npm start
```

Linux o Mac:

```bash
.venv/bin/python -m pip install -r pipeline/requirements.txt
npm start
```

Abrí http://127.0.0.1:8787

Si estás tocando la interfaz, `npm run dev` la sirve en el puerto 5174. El servidor de `npm start` tiene que estar corriendo.

## Idiomas

Español (rioplatense, España o México), inglés, portugués, francés, alemán, italiano, japonés, chino, coreano, ruso, árabe, hindi, polaco, neerlandés, turco, sueco, ucraniano y catalán.

## Límites

Hasta 45 minutos y 700 MB por video.

Google es opcional. En Mi portal se pega el Client ID de un cliente web, con origen `http://127.0.0.1:8787`.

## Lo que no subo

Las traducciones, las cuentas y las sesiones quedan en `data/`, en la máquina de cada quien. Esa carpeta no entra al repositorio.
