"""
Tareas periódicas que NO hacen esperar a la página: la revisión corre en un hilo aparte y la página muestra
siempre el último resultado conocido. El estado vive en el módulo, así que se comparte entre todas las
sesiones (una sola revisión para todos).
"""
import ctypes
import datetime as dt
import gc
import threading
import time

# Memoria: en Linux (Streamlit Cloud) cada hilo nuevo puede reservar su propia «arena» de memoria que luego no se
# devuelve al servidor, y la app se va inflando con cada revisión en segundo plano. Se limitan las arenas a 2 y,
# al terminar cada tarea, se devuelve al servidor la memoria libre. En Mac/Windows esto simplemente no hace nada.
try:
    _LIBC = ctypes.CDLL("libc.so.6")
    _LIBC.mallopt(-8, 2)        # M_ARENA_MAX = 2
except Exception:
    _LIBC = None


_ULTIMA_LIBERACION = [0.0]


def liberar_memoria_cada(segundos=60):
    """Llamada en cada recarga de página: como máximo una vez por minuto devuelve al servidor la memoria libre."""
    ahora = time.time()
    if ahora - _ULTIMA_LIBERACION[0] >= segundos:
        _ULTIMA_LIBERACION[0] = ahora
        liberar_memoria()


def liberar_memoria():
    """Recolecta basura y devuelve al sistema la memoria libre (malloc_trim)."""
    gc.collect()
    if _LIBC is not None:
        try:
            _LIBC.malloc_trim(0)
        except Exception:
            pass


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
        liberar_memoria()

    def obtener(self, *args):
        """Devuelve el último resultado al instante; si ya toca, lanza una nueva revisión en segundo plano."""
        with self._lock:
            if not self._corriendo and time.time() - self._ultimo_inicio >= self.intervalo:
                self._corriendo = True
                self._ultimo_inicio = time.time()
                threading.Thread(target=self._ejecutar, args=(args,), daemon=True).start()
            return self._resultado or {"ok": True, "pendiente": True, "hora": "…"}

    def invalidar(self):
        """La próxima llamada a obtener() lanza una revisión nueva."""
        with self._lock:
            self._ultimo_inicio = 0.0

    def ejecutar_ahora(self, *args):
        """Revisión inmediata (la página espera): para los botones «Sincronizar ahora»."""
        with self._lock:
            if self._corriendo:
                return self._resultado or {"ok": True, "pendiente": True, "hora": "…"}
            self._corriendo = True
            self._ultimo_inicio = time.time()
        self._ejecutar(args)
        return self._resultado