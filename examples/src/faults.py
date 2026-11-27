"""Real process stops and controlled contention using fresh interpreter processes."""
import multiprocessing
import os
import sqlite3

from gate import submit

CTX = multiprocessing.get_context("spawn")


def crash_child(path, args, point):
    def stop(position):
        if position == point:
            os._exit(86)
    submit(path, *args, hook=stop)


def crashed_submit(path, args, point):
    p = CTX.Process(target=crash_child, args=(path, args, point))
    p.start()
    p.join(20)
    if p.exitcode != 86:
        if p.is_alive():
            p.kill()
            p.join()
        raise RuntimeError(f"crash hook did not stop child at {point} (exit {p.exitcode})")
    return p.exitcode


def take(pipe):
    if not pipe.poll(20):
        raise RuntimeError("timed out waiting for contention probe")
    return pipe.recv()


def first_writer(path, args, channel):
    def pause(position):
        if position == "before_commit":
            channel.send("first staged")
            if take(channel) != "release":
                raise RuntimeError("bad release signal")
    channel.send(submit(path, *args, hook=pause))


def second_writer(path, args, channel):
    try:
        submit(path, *args, timeout=0)
    except sqlite3.OperationalError as e:
        if e.sqlite_errorcode != sqlite3.SQLITE_BUSY:
            raise
        channel.send(e.sqlite_errorname)
    else:
        raise RuntimeError("second writer acquired a reserved transaction")
    channel.send(submit(path, *args))


def overlap(path, first, second):
    parent1, child1 = CTX.Pipe()
    parent2, child2 = CTX.Pipe()
    p = CTX.Process(target=first_writer, args=(path, first, child1))
    q = CTX.Process(target=second_writer, args=(path, second, child2))
    p.start()
    try:
        if take(parent1) != "first staged":
            raise RuntimeError("first transaction missing")
        q.start()
        busy = take(parent2)
        parent1.send("release")
        answers = [take(parent1), take(parent2)]
        for worker in (p,q):
            worker.join(20)
            if worker.exitcode != 0:
                raise RuntimeError(f"writer failed: exit {worker.exitcode}")
        return busy, answers
    finally:
        for worker in (p,q):
            if worker.is_alive():
                worker.kill()
                worker.join()
        for pipe in (parent1, child1, parent2, child2):
            pipe.close()
