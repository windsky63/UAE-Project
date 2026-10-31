"""ASCII-named launcher so cmd.exe never needs to parse a Unicode script name."""

from app_paths import HOME, CODE
import importlib
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path


def main():
    module = importlib.import_module('\u53f0\u8d26')
    if '--check-launcher' in sys.argv[1:]:
        import tkinter
        interpreter = tkinter.Tcl()
        database = HOME / 'runtime' / 'ledger.sqlite3'
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            folders = db.execute('SELECT COUNT(*) FROM folders').fetchone()[0]
            documents = db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        print(json.dumps({'launcher': 'OK', 'python': sys.executable,
                          'tcl': interpreter.call('info', 'patchlevel'),
                          'folders': folders, 'documents': documents}, ensure_ascii=False))
        return
    window = module.tk.Tk()
    module.App(window)
    window.mainloop()


if __name__ == '__main__':
    main()
