# main.py - ESP32 + MicroPython
# Proyecto domotica + ciberseguridad: CAJA DETOX contra la tecnoadiccion
#
# Idea: el celu se guarda en una caja. Un sensor infrarrojo (FC-51) detecta si esta.
#  - Desde la app se inicia un "modo detox" de X minutos (solo si el celu esta guardado:
#    el sensor HABILITA el modo).
#  - Si sacas el celu antes de tiempo: LED rojo + buzzer + aviso en la app.
#  - Al cumplir el tiempo: pitidos de festejo y aviso "objetivo cumplido".
# Todo viaja por MQTT con TLS, usuario/contrasena y permisos (ACL) por topico.

import time
import ssl
import json
import network
import ntptime
import machine
import ubinascii
from machine import Pin
from umqtt.simple import MQTTClient

import secretos  # contrasenas: archivo aparte, nunca se comparte

# ---- Conexion (estos datos NO son secretos) ----
BROKER = "z5bfcca9.ala.us-east-1.emqxsl.com"
PUERTO = 8883                          # MQTT con TLS
USUARIO = "raspberry"                  # usuario de la placa en EMQX
TOPICO_CMD = b"casa/caja/cmd"          # la app manda ordenes aca
TOPICO_ESTADO = b"casa/caja/estado"    # la placa informa su estado aca

# ---- Pines ----
PIN_SENSOR = 27   # OUT del FC-51
PIN_VERDE = 25    # LED verde (con resistencia de 330 ohm)
PIN_ROJO = 26     # LED rojo  (con resistencia de 330 ohm)
PIN_BUZZER = 33   # buzzer activo

SENSOR_DETECTA_EN = 0   # el FC-51 pone su salida en 0 cuando detecta algo
BUZZER_SUENA_EN = 1     # si tu modulo de buzzer suena "al reves", cambia a 0
MINUTOS_MAX = 240       # nadie puede pedir un detox mas largo (validacion de entrada)

# MODO PRUEBA sin sensor: el boton BOOT de la placa simula el celu.
# Cada apretada alterna entre "guardado" y "afuera". Con el FC-51 conectado, poner False.
SIMULAR_SENSOR = False

# LED azul de la placa = estado de la conexion (sirve sin notebook):
#   parpadeo rapido = buscando WiFi | fijo = conectado al broker | apagado = error, reintentando
led_conexion = Pin(2, Pin.OUT, value=0)

sensor = Pin(PIN_SENSOR, Pin.IN, Pin.PULL_UP)
verde = Pin(PIN_VERDE, Pin.OUT, value=0)
rojo = Pin(PIN_ROJO, Pin.OUT, value=0)
buzzer = Pin(PIN_BUZZER, Pin.OUT, value=1 - BUZZER_SUENA_EN)

# ---- Estado ----
modo = "LIBRE"          # LIBRE / DETOX / ALERTA
celu_guardado = False
restante_ms = 0
meta_s = 0
salidas = 0             # cuantas veces sacaron el celu durante el detox
cliente = None
wlan = None             # interfaz WiFi (modo estacion = cliente de un access point)


def buzz(encendido):
    buzzer.value(BUZZER_SUENA_EN if encendido else 1 - BUZZER_SUENA_EN)


def pitidos(veces, duracion_ms=150):
    for _ in range(veces):
        buzz(True)
        time.sleep_ms(duracion_ms)
        buzz(False)
        time.sleep_ms(duracion_ms)


boton_boot = Pin(0, Pin.IN, Pin.PULL_UP)
celu_simulado = False
boton_antes = 1


def leer_sensor():
    global celu_simulado, boton_antes
    if SIMULAR_SENSOR:
        b = boton_boot.value()
        if boton_antes == 1 and b == 0:      # se acaba de apretar
            celu_simulado = not celu_simulado
        boton_antes = b
        return celu_simulado
    return sensor.value() == SENSOR_DETECTA_EN


def actualizar_salidas_fisicas(ahora):
    if modo == "ALERTA":
        parpadeo = (ahora // 300) % 2 == 0
        rojo.value(1 if parpadeo else 0)
        verde.value(0)
        buzz(parpadeo)
    elif modo == "DETOX":
        verde.value(1)
        rojo.value(0)
        buzz(False)
    else:  # LIBRE
        verde.value(1 if celu_guardado else 0)
        rojo.value(0)
        buzz(False)


def info_wifi():
    """Canal y senal del enlace 802.11 (para el monitoreo y la app)."""
    try:
        return wlan.config("channel"), wlan.status("rssi")
    except Exception:
        return None, None


def publicar_estado(evento=None, ping=None):
    if cliente is None:
        return
    canal, rssi = info_wifi()
    datos = {
        "celu": "GUARDADO" if celu_guardado else "AFUERA",
        "modo": modo,
        "restante": max(0, restante_ms // 1000),
        "meta": meta_s,
        "salidas": salidas,
        "canal": canal,
        "rssi": rssi,
    }
    if evento:
        datos["evento"] = evento
    if ping is not None:
        datos["ping"] = ping
    try:
        # retain=True: la app ve el ultimo estado apenas se conecta
        cliente.publish(TOPICO_ESTADO, json.dumps(datos), retain=True)
        print("Estado:", datos)
    except Exception as e:
        print("No se pudo publicar:", e)


def al_cambiar_celu(guardado):
    global modo, salidas
    if modo == "DETOX" and not guardado:
        modo = "ALERTA"
        salidas += 1
        publicar_estado("SACARON_CELU")
        return
    if modo == "ALERTA" and guardado:
        modo = "DETOX"
        publicar_estado("CELU_DEVUELTO")
        return
    publicar_estado()


def al_recibir(topico, msg):
    global modo, restante_ms, meta_s, salidas
    cmd = msg.decode().strip().upper()
    print("Comando recibido:", cmd)

    if cmd.startswith("INICIAR:"):
        try:
            minutos = int(cmd.split(":", 1)[1])
        except ValueError:
            print("Minutos invalidos, ignorado")
            return
        if minutos < 1 or minutos > MINUTOS_MAX:
            print("Minutos fuera de rango, ignorado")
            return
        if not celu_guardado:
            publicar_estado("NO_HAY_CELU")   # el sensor no habilita el modo
            return
        modo = "DETOX"
        meta_s = minutos * 60
        restante_ms = meta_s * 1000
        salidas = 0
        pitidos(1)
        publicar_estado("DETOX_INICIADO")

    elif cmd.startswith("PING:"):
        # Medicion de latencia: la app manda PING:<n> y mide cuanto tarda en volver
        n = cmd.split(":", 1)[1]
        if n.isdigit() and len(n) <= 10:
            publicar_estado("PONG", int(n))

    elif cmd == "TERMINAR":
        modo = "LIBRE"
        restante_ms = 0
        buzz(False)
        publicar_estado("DETOX_CANCELADO")

    else:
        print("Comando desconocido, ignorado")


# ---- Red y broker (igual que en la prueba del LED) ----
def mostrar_enlace(ssid):
    canal, rssi = info_wifi()
    print("Enlace 802.11: modo infraestructura (cliente/STA) | red:", ssid,
          "| canal:", canal, "| senal:", rssi, "dBm | IP:", wlan.ifconfig()[0])


def conectar_wifi():
    global wlan
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        try:
            mostrar_enlace(wlan.config("ssid"))
        except Exception:
            mostrar_enlace("(ya conectada)")
        return wlan
    for ssid, clave in secretos.REDES_WIFI:
        print("Probando WiFi:", ssid)
        wlan.connect(ssid, clave)
        for _ in range(20):
            led_conexion.value(not led_conexion.value())   # parpadeo: buscando WiFi
            if wlan.isconnected():
                print("WiFi OK:", ssid)
                mostrar_enlace(ssid)
                return wlan
            time.sleep(0.5)
        wlan.disconnect()
    raise OSError("No se pudo conectar a ninguna red WiFi")


def sincronizar_hora():
    for _ in range(3):
        try:
            ntptime.settime()
            return
        except Exception as e:
            print("NTP, reintento:", e)
            time.sleep(1)


def contexto_tls():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile="ca.crt")
    return ctx


def conectar_mqtt():
    id_cliente = b"esp32-" + ubinascii.hexlify(machine.unique_id())
    c = MQTTClient(
        client_id=id_cliente,
        server=BROKER,
        port=PUERTO,
        user=USUARIO,
        password=secretos.MQTT_PASS,
        keepalive=60,
        ssl=contexto_tls(),
    )
    c.set_callback(al_recibir)
    c.connect()
    c.subscribe(TOPICO_CMD)
    return c


# ---- Programa principal ----
celu_guardado = leer_sensor()
lectura_anterior = celu_guardado
ultimo_cambio = time.ticks_ms()

while True:
    try:
        conectar_wifi()
        sincronizar_hora()
        cliente = conectar_mqtt()
        led_conexion.value(1)   # fijo: conectado al broker
        print("Conectado al broker con TLS. Caja detox lista.")
        publicar_estado()

        anterior = time.ticks_ms()
        ultimo_ping = anterior
        ultimo_reporte = anterior

        while True:
            ahora = time.ticks_ms()
            dt = time.ticks_diff(ahora, anterior)
            anterior = ahora

            cliente.check_msg()

            # Sensor con antirrebote: el cambio tiene que durar 400 ms
            lectura = leer_sensor()
            if lectura != lectura_anterior:
                lectura_anterior = lectura
                ultimo_cambio = ahora
            elif lectura != celu_guardado and time.ticks_diff(ahora, ultimo_cambio) > 400:
                celu_guardado = lectura
                al_cambiar_celu(celu_guardado)

            # Cuenta regresiva: solo avanza con el celu guardado
            if modo == "DETOX":
                restante_ms -= dt
                if restante_ms <= 0:
                    modo = "LIBRE"
                    restante_ms = 0
                    pitidos(3)
                    publicar_estado("OBJETIVO_CUMPLIDO")

            actualizar_salidas_fisicas(ahora)

            # Mientras hay detox, informa cada 10 s para que la app este al dia
            if modo != "LIBRE" and time.ticks_diff(ahora, ultimo_reporte) > 10000:
                publicar_estado()
                ultimo_reporte = ahora

            # Cada 30 s: mantiene viva la conexion y muestra el estado del enlace en la consola
            if time.ticks_diff(ahora, ultimo_ping) > 30000:
                cliente.ping()
                canal, rssi = info_wifi()
                print("Monitor WiFi | canal:", canal, "| senal:", rssi, "dBm | broker: conectado (TLS)")
                ultimo_ping = ahora

            time.sleep_ms(50)

    except Exception as e:
        print("Error:", e, "- reintento en 5 s")
        led_conexion.value(0)   # apagado: se corto algo, reintentando
        buzz(False)
        try:
            cliente.disconnect()
        except Exception:
            pass
        cliente = None
        time.sleep(5)
