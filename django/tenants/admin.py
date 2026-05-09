from django.contrib import admin
from .models import Tenant

@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'plan_tier', 'status', 'created_at')
    search_fields = ('name', 'slug')
    list_filter = ('plan_tier', 'status')
    prepopulated_fields = {'slug': ('name',)}