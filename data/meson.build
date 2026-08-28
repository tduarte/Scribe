# Desktop entry
desktop_file = i18n.merge_file(
  input:       configure_file(
    input:  '@0@.desktop.in'.format(app_id),
    output: '@0@.desktop.in'.format(app_id),
    configuration: conf,
  ),
  output:      '@0@.desktop'.format(app_id),
  type:        'desktop',
  po_dir:      '../po',
  install:     true,
  install_dir: datadir / 'applications',
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

# AppStream metainfo
metainfo_file = i18n.merge_file(
  input:       configure_file(
    input:  '@0@.metainfo.xml.in'.format(app_id),
    output: '@0@.metainfo.xml.in'.format(app_id),
    configuration: conf,
  ),
  output:      '@0@.metainfo.xml'.format(app_id),
  po_dir:      '../po',
  install:     true,
  install_dir: datadir / 'metainfo',
)

appstream_util = find_program('appstreamcli', required: false)
if appstream_util.found()
  test('Validate appstream file', appstream_util,
       args: ['validate', '--no-net', '--explain', metainfo_file])
endif

# D-Bus activation, so the Background portal can relaunch us
configure_file(
  input:       '@0@.service.in'.format(app_id),
  output:      '@0@.service'.format(app_id),
  configuration: conf,
  install:     true,
  install_dir: datadir / 'dbus-1' / 'services',
)

# GSettings schema
install_data(
  '@0@.gschema.xml'.format(app_id),
  install_dir: datadir / 'glib-2.0' / 'schemas',
)

compile_schemas = find_program('glib-compile-schemas', required: false)
if compile_schemas.found()
  test('Validate schema file', compile_schemas,
       args: ['--strict', '--dry-run', meson.current_source_dir()])
endif

# Model catalog and stylesheet
install_data(['models.json', 'style.css'], install_dir: pkgdatadir)

# Icons
install_data(
  'icons/hicolor/scalable/apps/@0@.svg'.format(app_id),
  install_dir: datadir / 'icons' / 'hicolor' / 'scalable' / 'apps',
)
install_data(
  'icons/hicolor/symbolic/apps/@0@-symbolic.svg'.format(app_id),
  install_dir: datadir / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)

subdir('sounds')
