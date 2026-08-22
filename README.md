# Inventario IA

Aplicación web móvil para crear inventarios de propiedades a partir de videos, revisar objetos y exportar resultados.

## Capacidades actuales

- Registro, inicio de sesión persistente durante 30 días y cierre de sesión.
- Inventarios separados por usuario, país y moneda.
- Carga de videos desde celular con progreso visible.
- Carga reintentable en partes de 8 MB, apta para videos grandes detrás de Cloudflare.
- Procesamiento asíncrono y recuperable después de reiniciar el servicio.
- Análisis híbrido local: YOLO11n para conteo y Moondream 2 para muebles, decoración y categorías fuera de COCO.
- Edición, eliminación y carga manual de objetos y precios.
- Exportaciones CSV, XLSX y PDF.
- Precio sugerido y rango visibles automáticamente en cada objeto cuando existen comparables públicos.
- CSRF, cookies HttpOnly/SameSite, Argon2 y control de propiedad en cada operación.

## Puesta en marcha

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
chmod +x scripts/download_model.sh
./scripts/download_model.sh
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8810
```

Abrir `http://127.0.0.1:8810`.

Instancia publicada: `https://inventory.rodrantho.com`.

## Variables

- `INVENTARIO_COOKIE_SECURE=true`: obligatorio detrás de HTTPS.
- `INVENTARIO_ALLOW_REGISTRATION=true|false`: habilita registro público.
- `INVENTARIO_MAX_UPLOAD_MB=1024`: tamaño máximo por video.
- `INVENTARIO_DATA_DIR=/ruta`: base SQLite y videos.
- `INVENTARIO_YOLO_MODEL=/ruta/modelo.onnx`: modelo visual.

## Límites conocidos

El análisis semántico prioriza precisión sobre velocidad y corre por CPU. En el hardware actual una habitación demora aproximadamente 4–7 minutos. Las estimaciones dependen de comparables disponibles en el mercado configurado: Alemania usa publicaciones directas de Kleinanzeigen y los demás países degradan a resultados públicos de búsqueda. Si una fuente no responde, el objeto queda marcado sin comparables en lugar de asignarle un valor inventado. Para cobertura y estabilidad comercial en todos los países sigue siendo recomendable integrar APIs oficiales de marketplaces.
