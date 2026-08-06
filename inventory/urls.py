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
]
