from django.urls import path
from .views.checkout import SubscribeView
from .views.verify import VerifyPaymentView
from .views.webhooks import RazorpayWebhookView

urlpatterns = [
    path('subscribe/', SubscribeView.as_view(), name='subscribe'),
    path('verify/', VerifyPaymentView.as_view(), name='verify'),
    path('webhook/razorpay/', RazorpayWebhookView.as_view(), name='webhook_razorpay'),
]
