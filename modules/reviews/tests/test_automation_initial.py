"""Permission, due time and unsubscribe use the real Reviews SQL schema."""
import asyncio
import pytest
from cartray_reviews.automation_initial import due_initial, unsubscribe, unsubscribe_token
from test_automation_intake import setup, run
from test_automation_recipient import bind, consent

KEY=b'unsubscribe-synthetic-secret-0123456789'
def due(db, now=120):
    return asyncio.run(due_initial(db,store='store',environment='test',outbox_id=10,now=now))

def test_due_requires_time_matching_contact_and_permission_and_unsubscribe_is_idempotent():
    checkout,reviews=setup(); run(checkout,reviews)
    with pytest.raises(ValueError): due(reviews)
    bind(reviews)
    with pytest.raises(ValueError): due(reviews)
    consent(checkout,reviews)
    with pytest.raises(ValueError): due(reviews,now=109)
    assert due(reviews)['acquisition_id']=='order_1'
    token=unsubscribe_token(secret=KEY,store='store',environment='test',outbox_id=10)
    with pytest.raises(ValueError):
        asyncio.run(unsubscribe(reviews,store='store',environment='test',token=token[:-1]+'f',secret=KEY,now=120))
    for now in (120,121):
        asyncio.run(unsubscribe(reviews,store='store',environment='test',token=token,secret=KEY,now=now))
    assert reviews.db.execute('SELECT count(*) FROM review_invitation_consent_withdrawals').fetchone()[0]==1
    with pytest.raises(ValueError): due(reviews)
    with pytest.raises(ValueError): consent(checkout,reviews)
