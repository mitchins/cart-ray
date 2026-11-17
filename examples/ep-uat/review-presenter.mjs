// UAT presentation only. Native Reviews clients retain capability and retry handling.
import {presentNativeReviews} from './native-presenter.mjs';
const root = document.querySelector('[data-native-review]');
const page = root?.dataset.nativeReview;
if (page === 'review' || page === 'manage') {
  presentNativeReviews(page);
  const script = document.createElement('script');
  script.src = page === 'review' ? '/theme/native-reviews.js' : '/theme/native-manage.js';
  document.body.append(script);
}
