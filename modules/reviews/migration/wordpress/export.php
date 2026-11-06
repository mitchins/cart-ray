<?php
// Invoke with WP-CLI against an explicitly identified source. Output is private.
global $wpdb;
$expected = getenv('REVIEWS_EXPECTED_SOURCE_URL');
if (!$expected || get_option('home') !== $expected || get_option('siteurl') !== $expected) {
    fwrite(STDERR, "Source identity mismatch\n");
    exit(1);
}
if ($wpdb->query('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ') === false ||
    $wpdb->query('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY') === false) {
    fwrite(STDERR, "Could not start read-only snapshot\n");
    exit(1);
}
$selection = "(c.comment_type = 'review' OR c.comment_parent IN (SELECT r.comment_ID FROM {$wpdb->comments} r WHERE r.comment_type = 'review'))";
function reviews_query_or_fail($sql) {
    global $wpdb;
    $result = $wpdb->get_results($sql, ARRAY_A);
    if ($wpdb->last_error || !is_array($result)) {
        fwrite(STDERR, "Review source query failed\n");
        $wpdb->query('ROLLBACK');
        exit(1);
    }
    return $result;
}
$comments = reviews_query_or_fail("SELECT c.* FROM {$wpdb->comments} c WHERE $selection ORDER BY c.comment_ID");
$meta = reviews_query_or_fail("SELECT m.* FROM {$wpdb->commentmeta} m JOIN {$wpdb->comments} c ON c.comment_ID=m.comment_id WHERE $selection ORDER BY m.meta_id");
$subjects = reviews_query_or_fail("SELECT DISTINCT p.ID,p.post_type,p.post_status,p.post_name,p.post_title,p.post_parent FROM {$wpdb->posts} p JOIN {$wpdb->comments} c ON c.comment_post_ID=p.ID WHERE $selection ORDER BY p.ID");
$payload = wp_json_encode([
    'schema' => 'restricted.review-candidate.v1',
    'captured_utc' => gmdate('Y-m-d\TH:i:s\Z'),
    'site' => $expected,
    'complete' => false,
    'comments' => $comments,
    'commentmeta' => $meta,
    'subjects' => $subjects,
], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
if ($payload === false) {
    fwrite(STDERR, "Could not encode review candidate\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
echo $payload;
$wpdb->query('ROLLBACK');
