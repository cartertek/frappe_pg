#!/usr/bin/env python3
"""Add CI-only timing around ERPNext v16 after_install calls."""

from pathlib import Path

path = Path("apps/erpnext/erpnext/setup/install.py")
source = path.read_text()
marker = "def after_install():\n"
if marker not in source:
    raise SystemExit("ERPNext after_install() marker not found")
if "def _frappe_pg_timed_install_call" in source:
    raise SystemExit("ERPNext after_install() timing already installed")

helper = '''def _frappe_pg_timed_install_call(label, fn):
\timport time
\n\tstarted = time.perf_counter()
\ttry:
\t\treturn fn()
\tfinally:
\t\tprint(f"[frappe_pg install timing] {label}: {time.perf_counter() - started:.3f}s", flush=True)
\n\n'''
source = source.replace(marker, helper + marker, 1)
replacements = {
    '\tset_single_defaults()\n': '\t_frappe_pg_timed_install_call("set_single_defaults", set_single_defaults)\n',
    '\tsetup_repost_defaults()\n': '\t_frappe_pg_timed_install_call("setup_repost_defaults", setup_repost_defaults)\n',
    '\tcreate_print_setting_custom_fields()\n': '\t_frappe_pg_timed_install_call("create_print_setting_custom_fields", create_print_setting_custom_fields)\n',
    '\tcreate_marketing_campaign_custom_fields()\n': '\t_frappe_pg_timed_install_call("create_marketing_campaign_custom_fields", create_marketing_campaign_custom_fields)\n',
    '\tcreate_address_and_contact_custom_fields()\n': '\t_frappe_pg_timed_install_call("create_address_and_contact_custom_fields", create_address_and_contact_custom_fields)\n',
    '\tcreate_custom_company_links()\n': '\t_frappe_pg_timed_install_call("create_custom_company_links", create_custom_company_links)\n',
    '\tadd_all_roles_to("Administrator")\n': '\t_frappe_pg_timed_install_call("add_all_roles_to", lambda: add_all_roles_to("Administrator"))\n',
    '\tcreate_default_success_action()\n': '\t_frappe_pg_timed_install_call("create_default_success_action", create_default_success_action)\n',
    '\tcreate_incoterms()\n': '\t_frappe_pg_timed_install_call("create_incoterms", create_incoterms)\n',
    '\tcreate_default_role_profiles()\n': '\t_frappe_pg_timed_install_call("create_default_role_profiles", create_default_role_profiles)\n',
    '\tadd_company_to_session_defaults()\n': '\t_frappe_pg_timed_install_call("add_company_to_session_defaults", add_company_to_session_defaults)\n',
    '\tadd_standard_navbar_items()\n': '\t_frappe_pg_timed_install_call("add_standard_navbar_items", add_standard_navbar_items)\n',
    '\tadd_app_name()\n': '\t_frappe_pg_timed_install_call("add_app_name", add_app_name)\n',
    '\tupdate_roles()\n': '\t_frappe_pg_timed_install_call("update_roles", update_roles)\n',
    '\tmake_default_operations()\n': '\t_frappe_pg_timed_install_call("make_default_operations", make_default_operations)\n',
    '\tupdate_pegged_currencies()\n': '\t_frappe_pg_timed_install_call("update_pegged_currencies", update_pegged_currencies)\n',
    '\tset_default_print_formats()\n': '\t_frappe_pg_timed_install_call("set_default_print_formats", set_default_print_formats)\n',
    '\ttoggle_hidden_fields()\n': '\t_frappe_pg_timed_install_call("toggle_hidden_fields", toggle_hidden_fields)\n',
}
for old, new in replacements.items():
    if old not in source:
        raise SystemExit(f"ERPNext after_install call not found: {old.strip()}")
    source = source.replace(old, new, 1)
path.write_text(source)


# Also time the Frappe installer phases which run after ERPNext's own
# after_install hook. These are part of `bench install-app erpnext`.
installer = Path("apps/frappe/frappe/installer.py")
installer_source = installer.read_text()
installer_helper = """def _frappe_pg_timed_install_phase(label, fn):
\timport time

\tstarted = time.perf_counter()
\ttry:
\t\treturn fn()
\tfinally:
\t\tprint(f"[frappe_pg installer phase] {label}: {time.perf_counter() - started:.3f}s", flush=True)


"""
if "def _frappe_pg_timed_install_phase" not in installer_source:
    installer_source = installer_source.replace(
        "def install_app(name, verbose=False, set_as_patched=True, force=False):\n",
        installer_helper + "def install_app(name, verbose=False, set_as_patched=True, force=False):\n",
        1,
    )
    phase_replacements = {
        "\tsync_jobs()\n": '\t_frappe_pg_timed_install_phase("sync_jobs", sync_jobs)\n',
        "\tsync_fixtures(name)\n": '\t_frappe_pg_timed_install_phase("sync_fixtures", lambda: sync_fixtures(name))\n',
        "\tsync_customizations(name)\n": '\t_frappe_pg_timed_install_phase("sync_customizations", lambda: sync_customizations(name))\n',
        "\tsync_dashboards(name)\n": '\t_frappe_pg_timed_install_phase("sync_dashboards", lambda: sync_dashboards(name))\n',
    }
    for old, new in phase_replacements.items():
        if old not in installer_source:
            raise SystemExit(f"Frappe install phase not found: {old.strip()}")
        installer_source = installer_source.replace(old, new, 1)
    installer.write_text(installer_source)
