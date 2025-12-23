import os
import shutil
import time
from datetime import datetime
from tqdm import tqdm  # pip install tqdm
import threading
import itertools
import sys

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Caminhos de origem e destino
SOURCE = r"D:\Games\DOS"
DEST   = r"\\Julia\f\DOS"

# Flag global para encerrar animação
RUNNING = True

# -------- Utilidades -------- #

def ensure_folder(path: str):
    """Cria a pasta se não existir."""
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def is_temp_or_partial(path: str) -> bool:
    """Ignora arquivos temporários/parciais comuns em downloads/edições."""
    name = os.path.basename(path)
    lower = name.lower()
    if name.startswith("~$"):  # temp do MS Office
        return True
    temp_exts = (".tmp", ".crdownload", ".part", ".download", ".filepart", ".partial")
    return lower.endswith(temp_exts)


def wait_file_ready(path: str, retries: int = 20, delay: float = 0.5, stable_checks: int = 2) -> bool:
    """
    Espera o arquivo estar legível e com tamanho estável.
    - Tenta abrir em modo leitura binária.
    - Requer 'stable_checks' leituras consecutivas com o mesmo tamanho.
    """
    prev_size = None
    stable = 0

    for _ in range(retries):
        try:
            size = os.path.getsize(path)
            # tenta abrir para confirmar que não está bloqueado
            with open(path, "rb"):
                pass

            if prev_size is not None and size == prev_size:
                stable += 1
            else:
                stable = 0
            prev_size = size

            if stable >= (stable_checks - 1):
                return True
        except (PermissionError, FileNotFoundError):
            # ainda sendo gravado ou já foi removido
            pass
        time.sleep(delay)
    return False


def copy_with_progress(src_file: str, dest_file: str):
    """Copia arquivo mostrando barra de progresso em tempo real."""
    buffer_size = 1024 * 1024  # 1 MB
    total_size = os.path.getsize(src_file)

    ensure_folder(os.path.dirname(dest_file))

    with open(src_file, "rb") as fsrc, open(dest_file, "wb") as fdst, tqdm(
        total=total_size,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc=os.path.basename(src_file),
        ncols=80,
        leave=True,
        dynamic_ncols=True,
    ) as pbar:
        while True:
            buf = fsrc.read(buffer_size)
            if not buf:
                break
            fdst.write(buf)
            pbar.update(len(buf))

    shutil.copystat(src_file, dest_file, follow_symlinks=False)


def sync_file(src_file: str, counters: dict):
    """Sincroniza apenas um arquivo."""
    try:
        if not os.path.exists(src_file):  # pode ter sido removido durante o debounce
            return
        if is_temp_or_partial(src_file):
            return

        rel_dir = os.path.relpath(os.path.dirname(src_file), SOURCE)
        dest_dir = os.path.join(DEST, rel_dir)
        dest_file = os.path.join(dest_dir, os.path.basename(src_file))

        if not os.path.exists(dest_file):
            copy_with_progress(src_file, dest_file)
            print(f"🆕 Copiado: {src_file} → {dest_file}", flush=True)
            counters["novos"] += 1
        else:
            src_mtime = os.path.getmtime(src_file)
            dest_mtime = os.path.getmtime(dest_file)
            if src_mtime > dest_mtime or os.path.getsize(src_file) != os.path.getsize(dest_file):
                copy_with_progress(src_file, dest_file)
                print(f"🔄 Atualizado: {src_file} → {dest_file}", flush=True)
                counters["atualizados"] += 1
    except PermissionError:
        print(f"⚠️ Arquivo bloqueado (perm.): {src_file}", flush=True)
        counters["erros"] += 1
    except Exception as e:
        print(f"⚠️ Erro em {src_file}: {e}", flush=True)
        counters["erros"] += 1


def salvar_resumo(counters: dict, tempo: float):
    """Salva e mostra resumo final (chamado apenas no CTRL+C)."""
    resumo = (
        f"Resumo da sincronização:\n"
        f" - Novos arquivos: {counters['novos']}\n"
        f" - Atualizados: {counters['atualizados']}\n"
        f" - Erros: {counters['erros']}\n"
        f" - Tempo total: {tempo:.2f}s\n"
    )

    # Salva log na origem DEPOIS de parar o observer (não dispara eventos)
    log_filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".txt"
    log_path = os.path.join(SOURCE, log_filename)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(resumo)

    print("\n📄", resumo.strip())
    print(f"📄 Resumo salvo em {log_path}")


def animacao_monitoramento():
    """Animação contínua rodando em thread separada."""
    spinner = itertools.cycle(["|", "/", "-", "\\"])
    while RUNNING:
        sys.stdout.write(f"\r⏳ Monitorando... {next(spinner)}")
        sys.stdout.flush()
        time.sleep(0.2)


# -------- Watchdog Handler com Debounce + Espera -------- #

class SyncHandler(FileSystemEventHandler):
    def __init__(self, counters, debounce_seconds: float = 1.0):
        super().__init__()
        self.counters = counters
        self.debounce = debounce_seconds
        self._timers = {}          # path -> Timer
        self._lock = threading.Lock()

    def _schedule(self, path: str):
        """Agenda a sincronização do arquivo com debounce + wait_file_ready."""
        if os.path.isdir(path):
            return

        # aplica um pequeno atraso SEM bloquear a thread do watchdog
        def _run():
            # espera mais um pouco para o produtor terminar
            time.sleep(self.debounce)
            if not wait_file_ready(path, retries=20, delay=0.5, stable_checks=2):
                print(f"⚠️ Arquivo ainda em uso, ignorado: {path}", flush=True)
                with self._lock:
                    self._timers.pop(path, None)
                self.counters["erros"] += 1
                return
            sync_file(path, self.counters)
            with self._lock:
                self._timers.pop(path, None)

        with self._lock:
            # cancela timer anterior (coalescing de múltiplos eventos do mesmo arquivo)
            if path in self._timers:
                self._timers[path].cancel()
            t = threading.Timer(self.debounce, _run)
            self._timers[path] = t
            t.start()

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def shutdown(self):
        """Cancela timers pendentes para desligar com segurança."""
        with self._lock:
            for t in self._timers.values():
                try:
                    t.cancel()
                except Exception:
                    pass
            self._timers.clear()


# -------- Main -------- #

def main():
    global RUNNING
    print("🔎 Monitoramento iniciado (CTRL + C para encerrar)\n")

    counters = {"novos": 0, "atualizados": 0, "erros": 0}
    start_time = time.time()

    # Thread da animação
    t = threading.Thread(target=animacao_monitoramento, daemon=True)
    t.start()

    event_handler = SyncHandler(counters, debounce_seconds=1.0)
    observer = Observer()
    observer.schedule(event_handler, SOURCE, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        RUNNING = False
        observer.stop()
        observer.join()
        event_handler.shutdown()  # cancela timers

        tempo_total = time.time() - start_time
        salvar_resumo(counters, tempo_total)

        print("\n🛑 Monitoramento encerrado pelo usuário.")
        input("Pressione ENTER para sair...")


if __name__ == "__main__":
    main()
