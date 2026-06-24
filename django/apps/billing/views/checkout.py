from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from billing.services import create_subscription, update_subscription

class SubscribeView(APIView):
    def post(self, request):
        plan_id = request.data.get("plan_id")
        plan_tier = request.data.get("plan_tier")
        if not plan_id or not plan_tier:
            return Response({"error": "plan_id and plan_tier are required"}, status=status.HTTP_400_BAD_REQUEST)
        
        tenant = request.user.tenant
        try:
            if tenant.razorpay_subscription_id and tenant.plan_tier != "free":
                # Upgrade or Downgrade (Paid to Paid)
                # We schedule the change for the end of the current billing cycle
                update_subscription(tenant.razorpay_subscription_id, plan_id, schedule_change_at="cycle_end")
                return Response({
                    "status": "scheduled",
                    "message": "Plan change scheduled successfully."
                }, status=status.HTTP_200_OK)
            else:
                # New subscription (Free to Paid)
                subscription = create_subscription(tenant, plan_id, plan_tier)
                return Response({
                    "subscription_id": subscription["id"],
                    "razorpay_key_id": settings.RAZORPAY_KEY_ID
                }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
