-- During handover, an old workflow and the new order receipt must not both
-- acquire permission to send for the same order. Existing claims are retained.
CREATE TRIGGER post_purchase_receipt_handover BEFORE INSERT ON post_purchase_executions
WHEN EXISTS (
  SELECT 1 FROM post_purchase_executions e
  WHERE e.merchant_id = NEW.merchant_id AND e.environment = NEW.environment
    AND e.order_id = NEW.order_id
    AND ((NEW.workflow_key = 'order-receipt' AND e.workflow_key <> 'order-receipt')
      OR (NEW.workflow_key <> 'order-receipt' AND e.workflow_key = 'order-receipt'))
)
BEGIN
  SELECT RAISE(IGNORE);
END;
