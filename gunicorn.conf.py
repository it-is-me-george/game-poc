import fcntl

_lock_file = None


def post_fork(server, worker):
    global _lock_file
    from app import start_tick_thread
    _lock_file = open("/tmp/game_tick.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start_tick_thread()
    except OSError:
        _lock_file.close()
        _lock_file = None
