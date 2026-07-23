from django.contrib import admin

from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    Rack,
    RackVlanRange,
)


class RackVlanRangeInline(admin.TabularInline):
    model = RackVlanRange
    extra = 0


class NetworkSwitchAddressInline(admin.TabularInline):
    model = NetworkSwitchAddress
    extra = 0


class NetworkSwitchPortInline(admin.TabularInline):
    model = NetworkSwitchPort
    extra = 0


class NetworkDevicePortInline(admin.TabularInline):
    model = NetworkDevicePort
    extra = 0


@admin.register(VLAN)
class VLANAdmin(admin.ModelAdmin):
    list_display = ["name", "vlan_id", "subnet", "default_gateway", "dhcp_range"]
    search_fields = ["name", "vlan_id", "subnet"]
    ordering = ["vlan_id"]


@admin.register(Rack)
class RackAdmin(admin.ModelAdmin):
    list_display = ["name", "slot_count"]
    search_fields = ["name"]
    inlines = [RackVlanRangeInline]


@admin.register(NetworkSwitchType)
class NetworkSwitchTypeAdmin(admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]


@admin.register(NetworkSwitch)
class NetworkSwitchAdmin(admin.ModelAdmin):
    list_display = ["hostname", "switch_type", "serial_number", "rack", "rack_slot", "dhcp_server_enabled"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "switch_type"]
    inlines = [NetworkSwitchAddressInline, NetworkSwitchPortInline]


@admin.register(NetworkDeviceType)
class NetworkDeviceTypeAdmin(admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]


@admin.register(NetworkDevice)
class NetworkDeviceAdmin(admin.ModelAdmin):
    list_display = ["hostname", "device_type", "serial_number", "rack", "rack_slot"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "device_type"]
    inlines = [NetworkDevicePortInline]
