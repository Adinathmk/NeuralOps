import razorpay
from billing.services import verify_payment_signature
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView


class VerifyPaymentView(APIView):
    def post(self, request):
        payment_id = request.data.get("razorpay_payment_id")
        subscription_id = request.data.get("razorpay_subscription_id")
        signature = request.data.get("razorpay_signature")
        plan_tier = request.data.get("plan_tier")  # Frontend passes the target tier

        if not all([payment_id, subscription_id, signature, plan_tier]):
            return Response(
                {"error": "Missing required fields"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            # Verify signature
            verify_payment_signature(payment_id, subscription_id, signature)

            # Update tenant (this will trigger outbox signal and cache invalidation)
            tenant = request.user.tenant
            tenant.plan_tier = plan_tier
            tenant.razorpay_subscription_id = subscription_id
            tenant.save(update_fields=["plan_tier", "razorpay_subscription_id"])

            return Response(
                {"status": "Payment verified and subscription activated"},
                status=status.HTTP_200_OK,
            )
        except razorpay.errors.SignatureVerificationError:
            return Response(
                {"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return Response(
                {"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
