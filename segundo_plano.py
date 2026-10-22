"""
Tareas periódicas que NO hacen esperar a la página: la revisión corre en un hilo aparte y la página muestra
siempre el último resultado conocido. El estado vive en el módulo, así que se comparte entre todas las
sesiones (una sola revisión para todos).
"""
import datetime as dt
import threading
import time


class TareaPeriodica:
    def __init__(self, funcion, intervalo_seg):
        self.funcion = funcion
        self.intervalo = intervalo_seg
        self._lock = threading.Lock()
        self._resultado = None
        self._ultimo_inicio = 0.0
        self._corriendo = False

    def _ejecutar(self, args):
        try:
            res = self.funcion(*args)
        except Exception as ex:
            res = {"ok": False, "error": str(ex), "hora": dt.datetime.now().strftime("%H:%M")}
        with self._lock:
            self._resultado = res
            self._corriendo = False

    def obtener(self, *args):
        """Devuelve el último resultado al instante; si ya toca, lanza una nueva revisión en segundo plano."""
        with self._lock:
            if not self._corriendo and time.time() - self._ultimo_inicio >= self.intervalo:
                self._corriendo = True
                self._ultimo_inicio = time.time()
                threading.Thread(target=self._ejecutar, args=(args,), daemon=True).start()
            return self._resultado or {"ok": True, "pendiente": True, "hora": "…"}

    def ejecutar_ahora(self, *args):
        """Revisión inmediata (la página espera): para los botones «Sincronizar ahora»."""
        with self._lock:
            if self._corriendo:
                return self._resultado or {"ok": True, "pendiente": True, "hora": "…"}
            self._corriendo = True
            self._ultimo_inicio = time.time()
        self._ejecutar(args)
        return self._resultado