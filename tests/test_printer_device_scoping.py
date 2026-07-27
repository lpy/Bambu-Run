"""Printer shares the `infrastructure_device` table with non-printer devices
(NAS, routers, ...) in host projects like RAE. Printer queries must never
resolve one of those rows.
"""

import pytest
from django.urls import reverse

from bambu_run.models import Printer
from bambu_run.views import resolve_printer_from_request


@pytest.fixture
def logged_in_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="scoping", password="pw")
    client.force_login(user)
    return client


@pytest.fixture
def nas():
    """A non-printer device row sharing the table, sorting before any printer."""
    return Printer.all_objects.create(
        name="A NAS", model="DS920+", category="nas", is_active=True
    )


@pytest.mark.django_db
def test_default_manager_excludes_non_printers(nas):
    printer = Printer.objects.create(name="Z Printer", model="H2C", is_active=True)

    assert list(Printer.objects.all()) == [printer]
    assert nas in Printer.all_objects.all()


@pytest.mark.django_db
def test_new_printers_default_to_the_printer_category():
    printer = Printer.objects.create(name="Fresh", model="H2C")

    assert printer.category == Printer.CATEGORY_3D_PRINTER
    assert printer in Printer.objects.all()


@pytest.mark.django_db
def test_resolve_printer_skips_an_active_nas(nas):
    """The exact production failure: NAS sorts first and is active, printer is not."""
    printer = Printer.objects.create(name="Z Printer", model="H2C", is_active=False)

    assert resolve_printer_from_request(None) is None, "inactive printer must not resolve"

    printer.is_active = True
    printer.save()
    assert resolve_printer_from_request(None) == printer


@pytest.mark.django_db
def test_resolve_printer_by_pk_rejects_a_non_printer(nas):
    from django.http import Http404

    with pytest.raises(Http404):
        resolve_printer_from_request(nas.pk)


@pytest.mark.django_db
def test_dashboard_does_not_fall_back_to_a_nas(logged_in_client, nas):
    resp = logged_in_client.get(reverse("bambu_run:printer_dashboard"))

    assert resp.status_code == 200
    assert "error" in resp.context
    assert resp.context.get("printer_device") is None
    assert list(resp.context["all_printers"]) == []
