from django.contrib import admin

from .models import OutboxEvent, ProcessedEvent


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "topic", "key", "published", "created_at")
    list_filter = ("published", "topic", "created_at")
    search_fields = ("topic", "key", "event_id")
    readonly_fields = ("event_id", "topic", "key", "payload", "created_at", "published")

    def has_add_permission(self, request):
        return False  # Outbox rows are written by service layer, not manually

    def has_delete_permission(self, request, obj=None):
        return False  # Never delete outbox rows manually


@admin.register(ProcessedEvent)
class ProcessedEventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "consumer_group", "topic", "processed_at")
    list_filter = ("consumer_group", "topic")
    search_fields = ("event_id", "consumer_group", "topic")
    readonly_fields = ("event_id", "consumer_group", "topic", "processed_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
