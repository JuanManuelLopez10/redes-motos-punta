# Publicador automático — Instagram + Facebook (Motos Punta)

Automatiza la publicación de contenido en Instagram y Facebook **sin que tu PC
tenga que estar encendida**. El contenido vive en este repositorio de GitHub y
un proceso programado (GitHub Actions) lo publica solo, a la hora que indica el
nombre de cada archivo, y después lo mueve a la carpeta `Subidas/`.

---

## Cómo funciona (en 30 segundos)

1. Subís tus imágenes/videos a las carpetas `Feed/` y `Stories/`, con el nombre
   en formato **`DD-MM HH-MM`** (día-mes hora-minuto).
2. Cada 15 minutos, GitHub revisa si hay algo cuya hora ya llegó.
3. Lo que está listo se publica en Instagram **y** Facebook, y los archivos se
   mueven a `Feed/Subidas/` o `Stories/Subidas/`.

No pagás nada: GitHub Actions es gratis para esto, y el propio repositorio hace
de "hosting" de las imágenes (Meta las necesita en una URL pública para poder
descargarlas). **Por eso el repositorio tiene que ser público.**

---

## Convención de nombres de archivo

| Qué es | Cómo se nombra | Ejemplo |
|---|---|---|
| Foto simple (feed) | `DD-MM HH-MM.jpg` | `25-06 18-00.jpg` |
| Carrusel (feed) | `DD-MM HH-MM N` (N = orden) | `25-06 18-00 1.jpg`, `25-06 18-00 2.jpg`, … |
| Reel (feed) | video **sin** número | `25-06 18-00.mp4` |
| Historia (stories) | `DD-MM HH-MM` | `25-06 12-30.jpg` o `.mp4` |
| Caption / descripción | `DD-MM HH-MM.txt` (mismo nombre base) | `25-06 18-00.txt` |

Reglas:
- En **Feed**, si un archivo tiene número (`… 1`, `… 2`) es parte de un
  **carrusel** y el número es el orden. Un carrusel puede mezclar fotos y videos.
- En **Feed**, un **video sin número** es un **reel**.
- Una **imagen sin número** es una **foto simple**.
- El **caption** se lee del `.txt` con el mismo nombre base (sin el número de
  orden). Para un carrusel `25-06 18-00 1.jpg / 2.jpg`, el caption es
  `25-06 18-00.txt`. Si no hay `.txt`, se publica sin texto.
- Las historias no llevan caption (Instagram no lo muestra), así que el `.txt`
  en `Stories/` se ignora.
- Formatos soportados: imágenes `.jpg .jpeg .png`, videos `.mp4 .mov`.

> **Sobre la hora:** el año no va en el nombre; se asume el más cercano. El cron
> de GitHub no es exacto al minuto (puede demorar algunos minutos), así que
> tomá la hora del nombre como "a partir de", no como un horario clavado. Si
> subís algo con hora ya pasada, sale en la próxima corrida (dentro de ~15 min).

---

## Puesta en marcha (se hace una sola vez)

### Paso 1 — Crear el repositorio en GitHub

1. Entrá a <https://github.com/new>.
2. Nombre: `redes-motos-punta` (o el que quieras). Marcá **Public**.
3. Subí el contenido de esta carpeta al repo (podés arrastrar los archivos en
   "uploading an existing file", o usar GitHub Desktop si te resulta más cómodo).

### Paso 2 — Preparar las cuentas de Meta

Tu Instagram tiene que estar en modo **Business** o **Creator** y **vinculado a
una página de Facebook** (si ya manejás todo desde Meta Business Suite, lo más
probable es que ya esté así).

Necesitás tres datos. Te dejo cómo sacar cada uno.

**a) Crear una app en Meta for Developers**
1. Entrá a <https://developers.facebook.com/apps/> → **Crear app**.
2. Tipo de app: **Business**.
3. Dentro de la app, agregá el producto **Instagram Graph API** (y quedará
   disponible también lo de páginas de Facebook).

**b) Conseguir el token de acceso (recomendado: que no caduque)**

La forma robusta para algo que corre solo es un **System User token**, que no
expira:
1. Entrá a **Meta Business Suite → Configuración del negocio**
   (<https://business.facebook.com/settings>).
2. **Usuarios → Usuarios del sistema → Agregar** → creá uno (rol Admin).
3. Asignale **activos**: la página de Facebook y la cuenta de Instagram, con
   permiso de control total.
4. **Generar nuevo token** → elegí tu app → marcá estos permisos:
   `instagram_basic`, `instagram_content_publish`, `pages_show_list`,
   `pages_read_engagement`, `pages_manage_posts`.
5. Copiá el token (empieza con algo largo). **Guardalo bien, no se vuelve a
   mostrar.**

> Alternativa rápida (pero el token dura ~60 días y hay que renovarlo): usar el
> **Graph API Explorer** para generar un token de usuario con esos mismos
> permisos. Sirve para probar, pero para producción conviene el System User.

**c) Conseguir `IG_USER_ID` y `FB_PAGE_ID`**

Con el token en mano, abrí el **Graph API Explorer**
(<https://developers.facebook.com/tools/explorer/>) y hacé estas consultas:

- Tu página y su ID:  `me/accounts`  → anotá el `id` de la página (ese es
  `FB_PAGE_ID`).
- El ID de Instagram:  `{FB_PAGE_ID}?fields=instagram_business_account`  →
  el `id` que devuelve es tu `IG_USER_ID`.

### Paso 3 — Cargar los datos como "Secrets" del repo

En tu repo de GitHub: **Settings → Secrets and variables → Actions → New
repository secret**. Creá estos tres:

| Nombre del secret | Valor |
|---|---|
| `META_ACCESS_TOKEN` | el token del paso 2b |
| `IG_USER_ID` | el ID de Instagram (paso 2c) |
| `FB_PAGE_ID` | el ID de la página de Facebook (paso 2c) |

Los secrets están cifrados; nadie (ni vos después) los ve en texto plano, y no
quedan en el código.

### Paso 4 — Probar

1. En el repo, pestaña **Actions** → el workflow *"Publicar en Instagram y
   Facebook"* → **Run workflow** (eso lo dispara a mano, sin esperar al cron).
2. Mirá el log. Si algo falla, el mensaje de error de Meta aparece ahí.
3. Para una prueba sin publicar de verdad, podés correrlo en tu compu:
   `python publish.py --dry-run` (ver abajo).

Una vez que anda, ya queda solo: cada 15 minutos revisa y publica lo que toque.

---

## Tu flujo de trabajo diario

1. Preparás el contenido como siempre.
2. Lo subís al repo, a `Feed/` o `Stories/`, respetando los nombres.
   - Más fácil: instalá **GitHub Desktop**, cloná el repo en tu
     `Escritorio/Instagram`, y trabajá ahí como con una carpeta normal; cuando
     quieras, apretás "Commit" + "Push" y queda programado.
3. Listo. A la hora indicada se publica y el archivo pasa a `Subidas/`.

---

## Probar localmente (opcional)

```bash
pip install -r requirements.txt

# Ver qué se publicaría, sin tocar nada:
python publish.py --dry-run

# Simular que "ahora" son las 18:05 del 1 de octubre:
python publish.py --dry-run --now "01-10 18-05"
```

---

## Detalles y límites que conviene saber

- **El repo debe ser público** para que Meta pueda descargar las imágenes. Si te
  preocupa, no pongas nada sensible: son piezas de marketing que igual van a ser
  públicas al publicarse.
- **Carruseles en Facebook:** si un carrusel tiene videos mezclados con fotos,
  Facebook no admite ese formato mixto, así que en FB se publican **solo las
  fotos** del carrusel (en Instagram sí va completo). En el log queda avisado.
- **No hay duplicados:** el sistema registra en `state/published.json` qué se
  publicó en cada red. Si una corrida publica en Instagram pero falla Facebook,
  en la próxima **solo reintenta Facebook**, nunca repite Instagram.
- **Videos:** el reel debe cumplir lo de Instagram (vertical 9:16, 3–90 s).
  Si Meta lo rechaza, el error aparece en el log de Actions.
- **Renovar el token** (solo si usaste la alternativa de 60 días): regenerá el
  token y actualizá el secret `META_ACCESS_TOKEN`. Con System User no hace falta.
- **Apagar temporalmente** la automatización: en **Actions**, botón *"Disable
  workflow"*.

---

## Estructura del repositorio

```
.
├── Feed/                 # fotos, carruseles y reels a publicar
│   └── Subidas/          # acá caen los ya publicados
├── Stories/              # historias a publicar
│   └── Subidas/          # acá caen las ya publicadas
├── state/published.json  # registro de lo publicado (lo maneja el script)
├── publish.py            # el motor de publicación
├── requirements.txt
└── .github/workflows/publish.yml   # el programador (cron cada 15 min)
```
