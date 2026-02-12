import fcntl


def post_fork(server, worker):
    from app import start_tick_thread
    lock_file = open("/tmp/game_tick.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start_tick_thread()
    except OSError:
        pass
