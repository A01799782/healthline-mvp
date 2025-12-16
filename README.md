# Healthline MVP

App Flask + SQLite para registro básico de pacientes, medicamentos y tomas.

## Setup
1) Instalar dependencias: `pip install -r requirements.txt`
2) Ejecutar: `python3 app.py`
3) Abrir: http://localhost:5000

La base se crea como `healthline.db` en el root. Se puede cambiar con `HEALTHLINE_DB=/ruta/otra.db`.

## Simular avance de tiempo
Usa un offset en minutos: `HEALTHLINE_TIME_OFFSET_MINUTES=60 python3 app.py` simula que el reloj está 1 hora adelantado para cálculo de tomas y visualización.

## Flujo básico
- Crear paciente desde la lista.
- Entrar al detalle y agregar medicamento (frecuencia en horas, hora inicio y fin opcional).
- Ver próximas tomas y marcarlas como realizadas.
- Revisar la vista Alertas para un tablero global.

## Notas de borrado
Al eliminar un paciente se eliminan también sus medicamentos y tomas asociadas (sin huérfanos).
# healthline-mvp
# healthline-mvp
# healthline-mvp
