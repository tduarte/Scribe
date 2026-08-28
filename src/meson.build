# The launcher: Meson substitutes the install paths into main.py.in.
configure_file(
  input:         'main.py.in',
  output:        'scribe',
  configuration: conf,
  install:       true,
  install_dir:   bindir,
  install_mode:  'rwxr-xr-x',
)

# Application modules live in <pkgdatadir>/scribe, which main.py puts on
# sys.path so the flat imports between them resolve.
install_data(
  [
    '__init__.py',
    'application.py',
    'audio.py',
    'dictation.py',
    'history.py',
    'models.py',
    'postprocess.py',
    'sounds.py',
    'transcriber.py',
    'worker.py',
  ],
  install_dir: moduledir,
)

install_data(
  [
    'portals/__init__.py',
    'portals/background.py',
    'portals/base.py',
    'portals/inject.py',
    'portals/notify.py',
    'portals/shortcuts.py',
  ],
  install_dir: moduledir / 'portals',
)

install_data(
  [
    'ui/__init__.py',
    'ui/history_page.py',
    'ui/models_page.py',
    'ui/onboarding.py',
    'ui/preferences.py',
    'ui/window.py',
  ],
  install_dir: moduledir / 'ui',
)
