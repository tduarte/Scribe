project('scribe',
  version: '0.1.0',
  meson_version: '>= 1.0.0',
  license: 'GPL-3.0-or-later',
  default_options: ['warning_level=2'],
)

i18n   = import('i18n')
gnome  = import('gnome')
python = import('python')

py = python.find_installation('python3')

app_id     = 'io.github.tduarte.Scribe'
prefix     = get_option('prefix')
bindir     = prefix / get_option('bindir')
datadir    = prefix / get_option('datadir')
localedir  = prefix / get_option('localedir')
pkgdatadir = datadir / meson.project_name()
moduledir  = pkgdatadir / 'scribe'

conf = configuration_data()
conf.set('APP_ID',      app_id)
conf.set('VERSION',     meson.project_version())
conf.set('PYTHON',      py.full_path())
conf.set('localedir',   localedir)
conf.set('pkgdatadir',  pkgdatadir)

subdir('data')
subdir('src')
subdir('po')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)
