# WiFi Motion Lab v2

Detector de movimiento con la señal WiFi de tu laptop. Solo usa Python estándar: no necesitas instalar nada.

```bash
python wifi_lab.py
```

Se abre solo `http://localhost:8765`. Para cerrar, pulsa `Ctrl + C` en la terminal.

## Cómo calibrarlo bien (importante)

1. **Fondo (automático, 20 s):** al arrancar, quédate quieto/a o sal de la habitación. Si la calibración sale "ruidosa", pulsa **C** y repite.
2. **Distancia (recomendado, una sola vez):**
   - Ponte a ~0.5 m de la laptop, pulsa **N** y camina en el lugar durante 6 s.
   - Ponte a ~4 m, pulsa **L** y camina en el lugar durante 6 s.
   - Se guarda en `calibracion.json`. Si mueves la laptop o el router de sitio, repítelo.
3. **Plano:** haz **Shift + clic** donde está tu router y **clic** para añadir puntos de interés (Puerta, Cocina...).

## Controles

| Tecla / ratón | Acción |
|---|---|
| C | Recalibrar el fondo |
| N / L | Calibrar la distancia "cerca" / "lejos" |
| X | Volver a la calibración de distancia por defecto |
| + / - | Más / menos sensible |
| [ / ] | Achicar / agrandar el perímetro de alerta |
| M | Sonido on/off |
| T | Tráfico de apoyo on/off |
| D | Modo demo (persona simulada) |
| P | Borrar todos los puntos de interés |
| Clic / Shift + clic / clic derecho | Añadir punto / ubicar router / borrar punto |

## Qué hace y cuáles son sus límites

- **Distancia y tendencia:** el cuerpo perturba la señal mucho más cerca de la laptop que lejos. Con eso se estima la distancia y si la persona **se acerca o se aleja**. Es lo más fiable del programa.
- **Sonido:** cuanto más cerca, más fuerte, más agudo y más rápido (como un sensor de estacionamiento). Por debajo de ~0.6 m suena un tono continuo.
- **Plano:** cuando alguien pasa entre la laptop y el router, la señal baja. Así se sabe si está cerca de esa línea y cuándo la cruza. Con una sola antena **no se puede saber de qué lado está**, por eso se dibuja también la "posición espejo".
- **"Se dirige hacia":** compara cómo cambia la distancia a ti y a cada punto de interés. Para los puntos de interés es aproximado, porque depende del ángulo.
- **Una persona quieta se vuelve invisible** al cabo de unos segundos. Pasa con cualquier sensor basado en RSSI.
- **Tráfico de apoyo:** envía ~20 paquetes pequeños por segundo al router, porque muchas tarjetas WiFi solo actualizan la señal cuando reciben tráfico. En pantalla verás "lecturas X/s". Si es menor que 2, la detección será lenta.
- La lista de dispositivos son aparatos conectados a tu red, no personas.
