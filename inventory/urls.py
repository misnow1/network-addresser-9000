"""URL routes for the read-only UI (phase 15, ADR 0020).

Integer primary keys throughout, not business labels — a rack's ``name``
("WPC1SRU") and a VLAN's 802.1Q ``vlan_id`` (201) are both user-chosen
display strings, not stable lookup keys, and neither is guaranteed unique
the way a pk is. The admin's own URLs already work this way
(``admin:inventory_rack_change`` takes a pk), so this matches rather than
introduces a second addressing scheme.
"""

from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("", views.index, name="index"),
    path("racks/<int:pk>/", views.rack_detail, name="rack"),
    path("vlans/<int:pk>/", views.vlan_map, name="vlan_map"),
    path("devices/<int:pk>/", views.device_detail, name="device"),
    path("spares/", views.spare_pool, name="spares"),
    # Stage B (ADR 0020, phase 15) — read-parity + the audit trail. <slug>
    # is a plain str converter, not Django's `slug` type: the registry
    # itself is the source of truth for which values are valid, so an
    # unrecognised one 404s from inside the view rather than from a URL
    # pattern that would otherwise also (harmlessly, but confusingly)
    # accept characters no real slug ever contains.
    path("models/<str:slug>/", views.model_list, name="model_list"),
    path("models/<str:slug>/<int:pk>/", views.model_detail, name="model_detail"),
    path("audit/", views.audit, name="audit"),
]
