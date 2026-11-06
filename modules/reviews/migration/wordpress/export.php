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
$engine_sql = $wpdb->prepare(
    "SELECT TABLE_NAME,ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s,%s,%s,%s)",
    $wpdb->comments, $wpdb->commentmeta, $wpdb->posts, $wpdb->options
);
$engines = reviews_query_or_fail($engine_sql);
if (count($engines) !== 4 || count(array_filter($engines, function ($row) { return $row['ENGINE'] !== 'InnoDB'; })) !== 0) {
    fwrite(STDERR, "Review source tables are not all transactional\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
$identity_rows = reviews_query_or_fail("SELECT option_name,option_value FROM {$wpdb->options} WHERE option_name IN ('home','siteurl')");
$identity = [];
foreach ($identity_rows as $row) {
    $identity[$row['option_name']] = $row['option_value'];
}
if (count($identity) !== 2 || $identity['home'] !== $expected || $identity['siteurl'] !== $expected) {
    fwrite(STDERR, "Source identity mismatch inside snapshot\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
// Review plugins can create other contribution types. Do not silently omit
// unfamiliar source types: require an explicit disposition before capture.
$unsupported = reviews_query_or_fail("SELECT COUNT(*) n FROM {$wpdb->comments} WHERE COALESCE(comment_type,'') NOT IN ('review','comment','order_note')");
if ((int)$unsupported[0]['n'] !== 0) {
    fwrite(STDERR, "Additional review contribution types require reconciliation\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
// Traverse the complete descendant graph in the same repeatable-read snapshot.
// IDs are converted to integers before they are used in the SQL IN lists.
$root_rows = reviews_query_or_fail("SELECT comment_ID FROM {$wpdb->comments} WHERE comment_type='review' ORDER BY comment_ID LIMIT 10001");
if (count($root_rows) > 10000) {
    fwrite(STDERR, "Review source exceeds capture limit\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
$frontier = array_map('intval', array_column($root_rows, 'comment_ID'));
$seen = array_fill_keys($frontier, true);
while ($frontier) {
    $ids = implode(',', $frontier);
    $children = reviews_query_or_fail("SELECT comment_ID FROM {$wpdb->comments} WHERE comment_parent IN ($ids) ORDER BY comment_ID LIMIT 10001");
    if (count($children) > 10000) {
        fwrite(STDERR, "Review source exceeds capture limit\n");
        $wpdb->query('ROLLBACK');
        exit(1);
    }
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
$unclassified = reviews_query_or_fail("SELECT COUNT(*) n FROM {$wpdb->comments} c LEFT JOIN {$wpdb->posts} p ON p.ID=c.comment_post_ID WHERE (c.comment_type='comment' AND NOT ($selection) AND COALESCE(p.post_type,'')<>'post') OR (c.comment_type='order_note' AND COALESCE(p.post_type,'') NOT IN ('shop_order','shop_subscription'))");
if ((int)$unclassified[0]['n'] !== 0) {
    fwrite(STDERR, "Unclassified non-review comments require reconciliation\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
$review_meta_outside_graph = reviews_query_or_fail("SELECT COUNT(*) n FROM {$wpdb->commentmeta} m JOIN {$wpdb->comments} c ON c.comment_ID=m.comment_id WHERE c.comment_type='comment' AND NOT ($selection) AND (m.meta_key IN ('rating','verified','flag_count') OR LEFT(m.meta_key,6)='ivole_' OR LEFT(m.meta_key,3)='cr_' OR LOWER(m.meta_key) LIKE '%review%')");
if ((int)$review_meta_outside_graph[0]['n'] !== 0) {
    fwrite(STDERR, "Review metadata outside the selected graph requires reconciliation\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
$size = reviews_query_or_fail("SELECT COALESCE(SUM(OCTET_LENGTH(CONCAT_WS('',c.comment_author,c.comment_author_email,c.comment_author_url,c.comment_author_IP,c.comment_date,c.comment_date_gmt,c.comment_content,c.comment_agent,c.comment_type,c.comment_approved))),0) n FROM {$wpdb->comments} c WHERE $selection");
$meta_size = reviews_query_or_fail("SELECT COUNT(*) n,COALESCE(SUM(COALESCE(OCTET_LENGTH(m.meta_key),0)+COALESCE(OCTET_LENGTH(m.meta_value),0)),0) bytes FROM {$wpdb->commentmeta} m JOIN {$wpdb->comments} c ON c.comment_ID=m.comment_id WHERE $selection");
$subject_size = reviews_query_or_fail("SELECT COALESCE(SUM(OCTET_LENGTH(post_title)+OCTET_LENGTH(post_name)),0) bytes FROM (SELECT DISTINCT p.ID,p.post_title,p.post_name FROM {$wpdb->posts} p JOIN {$wpdb->comments} c ON c.comment_post_ID=p.ID WHERE $selection) subjects");
if ((int)$meta_size[0]['n'] > 100000 || (int)$size[0]['n'] + (int)$meta_size[0]['bytes'] + (int)$subject_size[0]['bytes'] > 8000000) {
    fwrite(STDERR, "Review source exceeds byte or metadata limit\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
$comments = reviews_query_or_fail("SELECT c.comment_ID,c.comment_post_ID,c.comment_author,c.comment_author_email,c.comment_author_url,c.comment_author_IP,c.comment_date,c.comment_date_gmt,c.comment_content,c.comment_karma,c.comment_approved,c.comment_agent,c.comment_type,c.comment_parent,c.user_id FROM {$wpdb->comments} c WHERE $selection ORDER BY c.comment_ID");
$meta = reviews_query_or_fail("SELECT m.meta_id,m.comment_id,m.meta_key,m.meta_value FROM {$wpdb->commentmeta} m JOIN {$wpdb->comments} c ON c.comment_ID=m.comment_id WHERE $selection ORDER BY m.meta_id LIMIT 100001");
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
if ($payload === false || strlen($payload) > 10000000) {
    fwrite(STDERR, "Could not encode review candidate\n");
    $wpdb->query('ROLLBACK');
    exit(1);
}
echo $payload;
$wpdb->query('ROLLBACK');
