# Caja Detox

Proyecto de domótica + ciberseguridad (Expo ITC) contra la tecnoadicción.

Guardás el celu en la caja, un sensor infrarrojo lo detecta y recién ahí podés iniciar un "modo detox" desde la app.
Si lo sacás antes de tiempo, se prende el LED rojo, suena el buzzer y la app avisa.

**App:** https://bielsaagustin-art.github.io/control-led/

## Arquitectura

```
[Celular: app web] --WSS+TLS :8084--> [Broker MQTT EMQX Cloud] <--MQTT+TLS :8883-- [Router / AP 802.11] <--WiFi-- [ESP32 en la caja]
```

- La app y la ESP32 nunca se conectan directo: las dos hacen conexiones salientes hacia el broker. Cero puertos abiertos.
- Arquitectura descentralizada: las reglas (habilitar el detox, alarma, cuenta regresiva) corren en la ESP32.

| Tópico | Publica | Escucha |
| --- | --- | --- |
| `casa/caja/cmd` | App (`INICIAR:<min>`, `TERMINAR`, `PING:<n>`) | ESP32 |
| `casa/caja/estado` | ESP32 (JSON: celu, modo, tiempo, canal, señal) | App |

## Seguridad

- WiFi WPA2-AES / WPA3 (nunca WEP)
- TLS en todo el camino; la ESP32 verifica el certificado del broker (`ca.crt`)
- Usuario y contraseña por dispositivo
- ACL de mínimo privilegio por tópico (todo lo demás, denegado)
- Contraseñas fuera del código: `secretos.py` no se publica (ver `secretos_ejemplo.py`)

## Hardware

| Componente | Pin ESP32 |
| --- | --- |
| Sensor IR FC-51 (OUT) | D27 |
| LED verde + 330 Ω | D25 |
| LED rojo + 330 Ω | D26 |
| Buzzer activo | D33 |
| LED azul de la placa (estado de conexión) | GPIO 2 |

## Archivos

- `index.html`: la app web (GitHub Pages)
- `main.py`: programa de la ESP32 (MicroPython)
- `secretos_ejemplo.py`: plantilla de configuración (copiar como `secretos.py` y completar)
