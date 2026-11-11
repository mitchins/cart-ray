import test from 'node:test';
import assert from 'node:assert/strict';
import {galleryImages,galleryIndex} from '../src/gallery.mjs';
test('gallery retains primary first and ordered extras with descriptive alt',()=>{
  assert.deepEqual(galleryImages({title:'Kit',image:'/primary.webp',images:[{url:'/side.webp',alt:'Side view'},{url:'/primary.webp',alt:'Front view'}]},'/shop/'),[{url:'/primary.webp',alt:'Front view'},{url:'/side.webp',alt:'Side view'}]);
});
test('single-image legacy products and relative preview paths remain usable',()=>{
  assert.deepEqual(galleryImages({title:'Kit',image:'assets/kit.svg',image_alt:'Kit contents'},'/minimal/'),[{url:'/minimal/assets/kit.svg',alt:'Kit contents'}]);
  assert.deepEqual(galleryImages({title:'Missing'}),[]);
});
test('remote image URLs remain intact and malformed entries are ignored',()=>{
  assert.deepEqual(galleryImages({title:'Kit',image_url:'https://example.com/primary.webp',images:[null,{}, {url:''}]}),[{url:'https://example.com/primary.webp',alt:'Kit — image 1'}]);
});
test('keyboard moves in both directions, wraps, and supports Home and End',()=>{
  assert.equal(galleryIndex(0,'ArrowLeft',3),2);assert.equal(galleryIndex(2,'ArrowRight',3),0);
  assert.equal(galleryIndex(1,'Home',3),0);assert.equal(galleryIndex(1,'End',3),2);
  assert.equal(galleryIndex(0,'Enter',3),null);
});
