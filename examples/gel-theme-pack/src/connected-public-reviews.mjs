// Public product reviews use the verified subject selected by the merchant build.
import {attachReviews} from './reviews.mjs';

const script = document.querySelector('#product-data');
if (script && window.CARTRAY_STOREFRONT?.reviewsEnabled === true) {
  const product = JSON.parse(script.textContent);
  if (product.review_subject) {
    attachReviews({reviewsEnabled:true, productKey:'product', reviewSubjects:{product:product.review_subject}});
  }
}
