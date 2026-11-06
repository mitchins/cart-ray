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
// Traverse the complete descendant graph in the same repeatable-read snapshot.
// IDs are converted to integers before they are used in the SQL IN lists.
$root_rows = reviews_query_or_fail("SELECT comment_ID FROM {$wpdb->comments} WHERE comment_type='review' ORDER BY comment_ID");
$frontier = array_map('intval', array_column($root_rows, 'comment_ID'));
$seen = array_fill_keys($frontier, true);
while ($frontier) {
    $ids = implode(',', $frontier);
    $children = reviews_query_or_fail("SELECT comment_ID FROM {$wpdb->comments} WHERE comment_parent IN ($ids) ORDER BY comment_ID");
    $frontier = [];
    foreach ($children as $child) {
        $id = intval($child['comment_ID']);
        if (!isset($seen[$id])) {
            $seen[$id] = true;
            $frontier[] = $id;
        }
    }
    if (count($seen) > 10000) {
        fwrite(STDERR, "Review source exceeds capture limit\n");
        $wpdb->query('ROLLBACK');
        exit(1);
    }
}
$selection = 'c.comment_ID IN (' . (count($seen) ? implode(',', array_keys($seen)) : '0') . ')';
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
