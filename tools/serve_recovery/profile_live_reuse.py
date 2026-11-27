"""Keep the accepted tracing-era record recoverable alongside real Live business."""
import copy
import hashlib

from tools.serve_recovery import ledger, profile_mcp_codec as codec

FORMAT = 'q4t-live-profile-source-reuse-v1'
RECORD_FORMAT = 'q4t-profile-business-v23'
BETA_FORMAT = 'q4t-live-beta-profile-source-reuse-v1'
BETA_RECORD_FORMAT = 'q4t-profile-business-v24'
AGENT_RECORD_FORMAT = 'q4t-profile-business-v25'
AGENT_FORMAT = 'q4t-agent-profile-source-reuse-v1'
AGENT_REFRESH = 'q4t-agent-profile-requalification-v1'
REGISTRATION_RECORD = 'q4t-profile-business-v26'
REGISTRATION_FORMAT = 'q4t-agent-registration-profile-source-v1'
SESSION_RECORD = 'q4t-profile-business-v27'
SESSION_FORMAT = 'q4t-agent-session-profile-source-v1'
VAULT_RECORD = 'q4t-profile-business-v28'
VAULT_FORMAT = 'q4t-agent-vault-profile-source-v1'
WORKSPACE_RECORD = 'q4t-profile-business-v31'
WORKSPACE_FORMAT = 'q4t-agent-workspace-source-v1'
ARTIFACT_RECORD = 'q4t-profile-business-v32'
ARTIFACT_FORMAT = 'q4t-session-artifact-source-v1'
MANAGED_TOOL_RECORD = 'q4t-profile-business-v33'
MANAGED_TOOL_FORMAT = 'q4t-managed-session-tools-source-v1'
PROGRAM_SDK_RECORD = 'q4t-profile-business-v89'
PROGRAM_SDK_FORMAT = 'q4t-program-sdk-reader-source-v1'
PROGRAM_SDK_RECEIPT = 'program-sdk-reader-handoff.json'
PROGRAM_SDK_REVIEWED = {}

COMPACT_MEDIA_RECORD = 'q4t-profile-business-v88'
COMPACT_MEDIA_FORMAT = 'q4t-compact-media-source-v1'
COMPACT_MEDIA_RECEIPT = 'compact-media-handoff.json'
COMPACT_MEDIA_REVIEWED = {
    'primary': 'd6cf6b10cf20fd9af51ebd45ff001a906ad9165a47528ca197bfd27c9d75b00a',
    'light': '4bb3af7504572a94678dd33869f18ad3d88116f9c4f8614b1dbf27ed629fbc91',
}

COMPACT_WINDOW_RECORD = 'q4t-profile-business-v87'
COMPACT_WINDOW_FORMAT = 'q4t-compact-window-source-v1'
COMPACT_WINDOW_RECEIPT = 'compact-window-handoff.json'
COMPACT_WINDOW_REVIEWED = {
    'primary': '3cc6f3e21fe4bc56e789c1dba8c4aee5854a9a275240e4a7acece3033f52b1b1',
    'light': 'b9ede4efceb00ccf7cbd46673cf75e00cb9acbbbf9db5d8969847a810fecf756',
}

MCP_ERROR_RECORD = 'q4t-profile-business-v86'
MCP_ERROR_FORMAT = 'q4t-mcp-error-reader-source-v1'
MCP_ERROR_RECEIPT = 'mcp-error-reader-handoff.json'
MCP_ERROR_REVIEWED = {
    'primary': '23a7dbe063d502452028b6c5c031ce46e1e478aab22b4ffac74361920907204e',
    'light': '0e2f5f1946f086ae4c468f21d3b3882b6ce5d1c1508eff1cb6153514b92260d4',
}

CACHE_RETENTION_RECORD = 'q4t-profile-business-v85'
CACHE_RETENTION_FORMAT = 'q4t-response-cache-retention-source-v1'
CACHE_RETENTION_RECEIPT = 'response-cache-retention-handoff.json'
CACHE_RETENTION_REVIEWED = {
    'primary': '56b9fc32ecd98a3678cd6d54c6efbf37b6ff355a1c5da7df65d102d23e42d767',
    'light': '8c44ff6c8bfe6ba6d84b0bbf123171f6c140e0ef7dd132d3716f7394c4ee6d53',
}

LAYER_RECORD = 'q4t-profile-business-v84'
LAYER_FORMAT = 'q4t-video-layer-source-v1'
LAYER_RECEIPT = 'video-layer-handoff.json'
LAYER_REVIEWED = {
    'primary': '7ff3cd08a248bd5b994142a9eaae14d61c6150bdece24af490dd44cdc60a6c09',
    'light': '4c627cddf1b60b1e131ca632e9a5645af8b93c01b8ffcf8ea5c3f621428d3877',
}

INTERRUPTED_RETRY_RECORD = 'q4t-profile-business-v83'
INTERRUPTED_RETRY_FORMAT = 'q4t-chatkit-interrupted-retry-source-v1'
INTERRUPTED_RETRY_RECEIPT = 'chatkit-interrupted-retry-handoff.json'
INTERRUPTED_RETRY_REVIEWED = {
    'primary': 'd383563ac564202a5290b5e7bc8d101b9dfa12faf6160d3ce26ac228ea79de49',
    'light': 'bfd253d23bfaa0904d809b7e27c581b3476d3368c9839cdeb7cfe159fa7a7851',
}

GRID_RECORD = 'q4t-profile-business-v82'
GRID_FORMAT = 'q4t-video-track-grid-source-v1'
GRID_RECEIPT = 'video-track-grid-handoff.json'
GRID_REVIEWED = {
    'primary': 'c5aa3df34136466d52e8b2c9c4d59950cc74ba584ad152938fb761596db1de03',
    'light': 'b5ee96a9a4deb6a783661eaecf75cfdb3f4cca962408c73ced1fe94a66878786',
}

FORK_RECORD = 'q4t-profile-business-v81'
FORK_FORMAT = 'q4t-program-fork-source-v1'
FORK_RECEIPT = 'program-fork-handoff.json'
FORK_REVIEWED = {
    'primary': '9d1040ded9d883e8e7ea34836bd3f6f45dfc4bb9ee541d7346c2678862c76910',
    'light': 'a9730796f469417ed76b068f67b07ee28aa1290c1f412f746d249fbbd3c03637',
}

CLIENT_SHELL_RECORD = 'q4t-profile-business-v80'
CLIENT_SHELL_FORMAT = 'q4t-client-shell-source-v1'
CLIENT_SHELL_RECEIPT = 'client-shell-handoff.json'
CLIENT_SHELL_REVIEWED = {
    'primary': 'a7c560d2d82131d5c12af7fa9a10596c1fe14c27a4f535c27854d8f41ca6b72a',
    'light': '89c6d8e36fd13e8a99b0e228672e88d72a32a89f744a034f0ee6a3b9f2234124',
}

PATCH_RECORD = 'q4t-profile-business-v79'
PATCH_FORMAT = 'q4t-client-patch-source-v1'
PATCH_RECEIPT = 'client-patch-handoff.json'
PATCH_REVIEWED = {
    'primary': '0c2594edafea9df9943d219a8b3ddf093f1298cebbc048c7e6b5966ea372eb2b',
    'light': '7e9975255a9f3695feca9cc2256eb0c0b38190ea128bd3e040702adfb6734d2b',
}

LOCAL_SHELL_RECORD = 'q4t-profile-business-v78'
LOCAL_SHELL_FORMAT = 'q4t-local-shell-source-v1'
LOCAL_SHELL_RECEIPT = 'local-shell-handoff.json'
LOCAL_SHELL_REVIEWED = {
    'primary': 'c07ac86d9a8ee13907f8fa2543a20e431363dd97e85c1ee3426608ac5aa2c1d2',
    'light': '6d7e4ab4489bb12efbc3979b8799d9b7684c13631a4fdbe33bfb60b223427bde',
}

APPROVAL_IDS_RECORD = 'q4t-profile-business-v77'
APPROVAL_IDS_FORMAT = 'q4t-approval-identifiers-source-v1'
APPROVAL_IDS_RECEIPT = 'approval-identifiers-handoff.json'
APPROVAL_IDS_REVIEWED = {
    'primary': 'b876117bd4ae572690faa04d5beaada2224388f74eba64d87ca73f2f555fe09b',
    'light': '7d9e387e7da63b792710a06bd0b43cc2c71e26f15e630bdad21014bb80bb3a11',
}

COMPACT_RECORD = 'q4t-profile-business-v76'
COMPACT_FORMAT = 'q4t-conversation-compaction-source-v1'
COMPACT_RECEIPT = 'conversation-compaction-handoff.json'
COMPACT_REVIEWED = {
    'primary': '01456f89abc4efe5ba8d3b500d6e62a86967f231e17312cb3a9dba7c33535af8',
    'light': 'bfa36caadd7e7b075d41d2addaacd06c816d82b94797ae35481b9a4857dc8b34',
}

PARTS_RECORD = 'q4t-profile-business-v75'
PARTS_FORMAT = 'q4t-stored-content-parts-source-v1'
PARTS_RECEIPT = 'stored-content-parts-handoff.json'
PARTS_REVIEWED = {
    'primary': '61b4bdfc5885171b7138eed9e071d8a40d421959f7f038362852689ead3249a5',
    'light': '955eb53527942fde0b6aed319c1f86a80a7c658d846c5b9186fde097afec497e',
}

NULL_HISTORY_RECORD = 'q4t-profile-business-v74'
NULL_HISTORY_FORMAT = 'q4t-null-assistant-history-source-v1'
NULL_HISTORY_RECEIPT = 'null-assistant-history-handoff.json'
NULL_HISTORY_REVIEWED = {
    'primary': 'cdc43682c65e2d905cbfef6fb3f5f9f146812bf978db4219f91f6d4e21401d3a',
    'light': 'f83427f21c79690bc278413a1def13d054b7196488dc0ec559c4300efbf14083',
}

TOOL_MAP_RECORD = 'q4t-profile-business-v73'
TOOL_MAP_FORMAT = 'q4t-tool-map-source-v1'
TOOL_MAP_RECEIPT = 'tool-map-handoff.json'
TOOL_MAP_REVIEWED = {
    'primary': '91353897f1b88dd4916281832177ed7ee542ef1a8e6c81a25578b7241f8c149a',
    'light': '3f4f1574bd3d077409bd418a6d50ba49fa0b60619962f3cc82962ddaedbd3bf5',
}

REGION_RECORD = 'q4t-profile-business-v72'
REGION_FORMAT = 'q4t-quickdraw-region-source-v1'
REGION_RECEIPT = 'quickdraw-region-handoff.json'
REGION_REVIEWED = {
    'primary': '39a046137baf97478e5f9b26505ba965671eefc4e73416479e53055d9b91c61a',
    'light': 'fcc7d3e93d1c3371e7916e9c27a6a22d41019c1865a3bffd3336ac91306c1b25',
}

VIDEO_CLIP_RECORD = 'q4t-profile-business-v71'
VIDEO_CLIP_FORMAT = 'q4t-video-clip-source-v1'
VIDEO_CLIP_RECEIPT = 'video-clip-handoff.json'
VIDEO_CLIP_REVIEWED = {
    'primary': 'a01b774721c1b207b148a3704fd5b7c7bda25c8d05ec60dc2205b3602015f4fa',
    'light': '96d044ff214120db59b9dc5f7151f1375035492333f7728ef8e486851e8b80b0',
}
CLIP_CAPABILITY = dict(track_grid='whole-pixel', source='quicktime-movie-and-track',
                       exterior='black', frozen_conversion='q4t.cpu_video_png.v6')

RESPONSE_FAILURE_RECORD = 'q4t-profile-business-v70'
RESPONSE_FAILURE_FORMAT = 'q4t-response-failure-source-v1'
RESPONSE_FAILURE_RECEIPT = 'response-failure-handoff.json'
RESPONSE_FAILURE_REVIEWED = {
    'primary': '9396f0b67a256784879cd37957a835591f1ec6d5e7a01427563f08f662f30dc7',
    'light': '6aee1dbf9a8f9544b348302f16144cabf5790a1f745b05633735194a5a4c9392',
}

EVAL_CANCEL_RECORD = 'q4t-profile-business-v69'
EVAL_CANCEL_FORMAT = 'q4t-eval-cancel-source-v1'
EVAL_CANCEL_RECEIPT = 'eval-cancel-handoff.json'
EVAL_CANCEL_REVIEWED = {
    'primary': 'b21b02f1f089fc337db9c373ba1066e9464785a89c768fb38f75dd267d639ba0',
    'light': '9053cbc75155ea539fbb4896f1daa974248157204e440fd95c2b0baae96d7ad5',
}

FRAME_RECORD = 'q4t-profile-business-v68'
FRAME_FORMAT = 'q4t-visual-frame-source-v1'
FRAME_RECEIPT = 'visual-frame-handoff.json'
FRAME_REVIEWED = {
    'primary': '53733256f15b3801f7d5b1713d532b940e3a5db318621607226b4c94944246da',
    'light': 'ae46ec08ec7cd100300781c098f96fe446cabad890d6b37203d22dc9a9032e49',
}

VISUAL_ANSWER_RECORD = 'q4t-profile-business-v67'
VISUAL_ANSWER_FORMAT = 'q4t-visual-answer-source-v1'
VISUAL_ANSWER_RECEIPT = 'visual-answer-handoff.json'
VISUAL_ANSWER_REVIEWED = {
    'primary': '59d8f260fb2d4f987220e19f9ae46c141ce8f9ddce6817575eed296465d5dc01',
    'light': '297d0c4e991ac54189ae2eaca2a09626c4f092ca7ad974dd53e023d5fce4b51a',
}

MEDIA_HANDOFF_RECORD = 'q4t-profile-business-v66'
MEDIA_HANDOFF_FORMAT = 'q4t-response-media-source-v1'
MEDIA_HANDOFF_RECEIPT = 'response-media-handoff.json'
MEDIA_HANDOFF_REVIEWED = {
    'primary': '48771b76e80864cb381a912c193891baacd24250d2a2498bab2c05bd72c82700',
    'light': '81c9607df693cb15f854d4909bc9392801bee4269b4880d8d16f12d8f1d3c1b5',
}

SESSION_CONTROL_RECORD = 'q4t-profile-business-v65'
SESSION_CONTROL_FORMAT = 'q4t-agent-session-control-source-v1'
SESSION_CONTROL_RECEIPT = 'agent-session-control-handoff.json'
SESSION_CONTROL_PILOT_REVIEWED = {
    'primary': 'c19e8f803424de7b8f595afc4d8c67a172b79708ef9ec9b0a3efd900512c5fe8',
    'light': '786810a13ebf613cbca5027f05a3e6c1265f8ee238a35734b203aa364d45fbc2',
}

SESSION_CONTROL_RECOVERY_REVIEWED = {
    'primary': 'c08b3f86473596fbc84bb1a12faa802bdc27f7476e0cee11a6e0cc4cffee7e07',
    'light': 'eb90580a6d0b0a7068ac53b94c2ae0efb0dfa51949ef95796a8106f9112ee998',
}

SESSION_CONTROL_REVIEWED = {
    'primary': '68f7313537f01bae223b8735e4d7514753a743acd4b259b94ccc12e350549ce4',
    'light': '5f92707475a6a04a32ccc13d3b9eeb64cf8eef141c545fa375749fd03c54adc2',
}

CHATKIT_WIDGET_RECORD = 'q4t-profile-business-v64'
CHATKIT_WIDGET_FORMAT = 'q4t-chatkit-widget-source-v1'
CHATKIT_WIDGET_RECEIPT = 'chatkit-widget-handoff.json'
CHATKIT_WIDGET_REVIEWED = {
    'primary': 'a4fe7f963ef2c7735699f03e2a56c756e53d534f3fb7f113aa02cc318cd7491a',
    'light': 'cbd157c6529affc3563089ac7861dbfa6fca647b48ac7d50f9bc0562fe621924',
}
CHATKIT_IMAGE_RECORD = 'q4t-profile-business-v63'
CHATKIT_IMAGE_FORMAT = 'q4t-chatkit-image-source-v1'
CHATKIT_IMAGE_RECEIPT = 'chatkit-image-handoff.json'
CHATKIT_IMAGE_REVIEWED = {
    'primary': '5bf8d9d0665c87064807973f340efdb2b451b3a7b62875bc8d341f1685cdb19d',
    'light': '74a820ae18b2231ea817fe7c1307a2f6db685f4e87e8d0919d73e68b9b20746a',
}
CHATKIT_WORKFLOW_RECORD = 'q4t-profile-business-v62'
CHATKIT_WORKFLOW_FORMAT = 'q4t-chatkit-workflow-source-v1'
CHATKIT_WORKFLOW_RECEIPT = 'chatkit-workflow-handoff.json'
CHATKIT_WORKFLOW_REVIEWED = {
    'primary': 'e7b73f093b01a9b74b97cad1417eb2df133ed447d4ee8c6b85e2f1a0af64e958',
    'light': '094242eb2be6752860004eb78f4442e4f0aa3dc45754c5fc55c5ac00b6810375',
}
CHATKIT_GENERATION_RECORD = 'q4t-profile-business-v61'
CHATKIT_GENERATION_FORMAT = 'q4t-chatkit-generation-source-v1'
CHATKIT_GENERATION_RECEIPT = 'chatkit-generation-handoff.json'
CHATKIT_GENERATION_REVIEWED = {
    'primary': '858b8111be51b5378b5f16082843dad6d2abbdf56e3366bee1d7f64a82b19ea4',
    'light': 'b79a92d5003ddb295b50c00849425eccfc55c5674c0554550d89b64ac7bde609',
}
CHATKIT_BROWSER_RECORD = 'q4t-profile-business-v60'
CHATKIT_BROWSER_FORMAT = 'q4t-chatkit-browser-output-source-v1'
CHATKIT_BROWSER_RECEIPT = 'chatkit-browser-output-handoff.json'
CHATKIT_BROWSER_REVIEWED = {
    'primary': 'acdcd284eb75f69e9dc7f0b85e75d4146edf049868b367e7935be4703d2fa34b',
    'light': 'ce580aca9e8f74bba813d7bc9bec97e2668278c54564db66d47601e84c4b5933',
}
CHATKIT_ACTIONS_RECORD = 'q4t-profile-business-v59'
CHATKIT_ACTIONS_FORMAT = 'q4t-chatkit-actions-source-v1'
CHATKIT_ACTIONS_RECEIPT = 'chatkit-actions-handoff.json'
CHATKIT_ACTIONS_REVIEWED = {
    'primary': '634261efdd85893eb3c778c59f7648e1f897b222826c871a30b3278a5bafdabc',
    'light': '95156e11971866c4e86909838931857929931f6a94bc4638d3cc3f1869626892',
}
CHATKIT_RETRY_RECORD = 'q4t-profile-business-v58'
CHATKIT_RETRY_FORMAT = 'q4t-chatkit-retry-source-v1'
CHATKIT_RETRY_RECEIPT = 'chatkit-retry-handoff.json'
CHATKIT_RETRY_REVIEWED = {
    'primary': 'd12779c9764a3fd17a59a01c62cbf05b967fe43aa0dd58dbfec6197d0932d805',
    'light': '9850ef16382e3717158652263f8e1867976c4e347684e1b577b799f22129a9e7',
}
STORED_REFUSAL_RECORD = 'q4t-profile-business-v57'
STORED_REFUSAL_FORMAT = 'q4t-stored-refusal-source-v1'
STORED_REFUSAL_RECEIPT = 'stored-refusal-handoff.json'
STORED_REFUSAL_REVIEWED = {
    'primary': '111af0f29262039f80b38f93f3dec904f0245f22655f827f96eb3cccd7d2775a',
    'light': 'dc83d48ae79f951b0b3c9ebdf957f7e6c29459b2eda52c97adb397286e4f6961',
}
TYPED_HISTORY_RECORD = 'q4t-profile-business-v56'
TYPED_HISTORY_FORMAT = 'q4t-typed-function-history-source-v1'
TYPED_HISTORY_RECEIPT = 'typed-history-handoff.json'
TYPED_HISTORY_REVIEWED = {
    'primary': 'a20dcaffedf0867ddbf7a0db9ab85fc96a9a626dfd61f1d7716b31f21272b057',
    'light': 'aaf1cffc4688eac8ecfb65185606db573ee85b49e55507ec245fa5756aa931ad',
}
TRACK_RECORD = 'q4t-profile-business-v55'
TRACK_FORMAT = 'q4t-video-track-source-v1'
TRACK_RECEIPT = 'alternate-track-handoff.json'
TRACK_REVIEWED = {
    'primary': 'f8c40e77e4eb07b40db75cbb2dc6c7a335a890eba18edd9872f5e0bea72147e8',
    'light': '0f5f5681a84f223721d9c64aaab1276fcf10f293c04dbf078a97254fb3a4fbc1',
}
CHARACTER_RECORD = 'q4t-profile-business-v54'
CHARACTER_FORMAT = 'q4t-video-character-source-v1'
CHARACTER_RECEIPT = 'video-character-handoff.json'
CHARACTER_REVIEWED = {
    'primary': '0383b5f9b4029b4df1d5f2d17d1d605c213cfb8e025fd55d9a309c16cf82c9de',
    'light': 'd86869c161d3c401ed6a70278644983d9dba262439b2842d45fec11154f03518',
}
CHARACTER_CAPABILITY = dict(endpoint='/v1/videos/characters', operations=['create', 'retrieve'],
                            scope='authenticated_client', source='original_encoded_clip',
                            max_name_characters=80, max_source_bytes=32 << 20, decoder_compiled=True)
COMPOSER_RECORD = 'q4t-profile-business-v53'
COMPOSER_FORMAT = 'q4t-chatkit-dictation-source-v1'
COMPOSER_RECEIPT = 'chatkit-dictation-handoff.json'
COMPOSER_REVIEWED = {
    'primary': 'a2a84dafd898a1ed1c8bae6cb6cd6333ff0a5dc6cb7f689befc106a604b36c10',
    'light': 'e3f170a3f64539e75ce3902f37f05d07f47cdc54fdebfcd3ed4492b4c8f6dbc9',
}
PAGINATION_RECORD = 'q4t-profile-business-v52'
PAGINATION_FORMAT = 'q4t-assistant-pagination-source-v1'
PAGINATION_RECEIPT = 'assistant-pagination-handoff.json'
PAGINATION_REVIEWED = {
    'primary': '10c882afa094a33b3608843a03eb90eb4a366c8f9b20d9100ed110f67b38e203',
    'light': '2820381c6a1cc285b5df2c1bfe6aafaee42b3caf06092877c4b43d23d8c2673e',
}
UPLOAD_RECORD = 'q4t-profile-business-v51'
UPLOAD_FORMAT = 'q4t-chatkit-upload-source-v1'
UPLOAD_REVIEWED = {
    'primary': 'ba60a7d3382d53de343add8600da9e35b71165744f946b8fed89e3ad1a1c9ead',
    'light': '24d393e5ae0b296c417dabc6e321cf81eb2fdfbb2f1d5d41c7f0ff9373b42876',
}
FEEDBACK_RECORD = 'q4t-profile-business-v50'
FEEDBACK_FORMAT = 'q4t-chatkit-feedback-source-v1'
FEEDBACK_REVIEWED = {
    'primary': 'b669f8e7ca99498b93481f15aab01544a42f8779ae81abf77cf3d10d0683953a',
    'light': 'b8c53412e568c6a9e8cd60914c2a5050114310f0118ddfd5d6ebfa8f5e11b82f',
}
METEOR_RECORD = 'q4t-profile-business-v49'
METEOR_FORMAT = 'q4t-eval-meteor-source-v1'
METEOR_REVIEWED = {
    'primary': 'd6c855feb1a35f78850a47153d5f619bf85fa9d390a14de9ab3682f78c20d658',
    'light': '79cb68f51944eeca14ca6c5b30e39ab97ec7851d06f33141827b3377951dc965',
}
HISTORY_RECORD = 'q4t-profile-business-v48'
HISTORY_FORMAT = 'q4t-historical-arguments-source-v1'
HISTORY_REVIEWED = {
    'primary': '953bcf79d1190b8e7e138ffbd146ebed89d58a472ad792fcfd099717bdce772c',
    'light': '8480702068d8d07cc56c9ce3a9560ebf3b5725e5e585d53ad71c7134fd43888f',
}
ISSUED_TOOL_HISTORY = dict(identified_results='complete_unique_batch',
    unordered_text_results=True, unordered_media_results=False,
    legacy_without_ids=True, legacy_shape_validation=True,
    arguments='required_object_or_json_object_text',
    result_content='required_string_or_array')
REPAIRED_TOOL_HISTORY = dict(ISSUED_TOOL_HISTORY,
    arguments='object_or_original_identified_argument_text',
    unidentified_arguments=ISSUED_TOOL_HISTORY['arguments'],
    non_mapping_text='quoted_assistant_history')
SAFETY_RECORD = 'q4t-profile-business-v47'
SAFETY_FORMAT = 'q4t-project-safety-source-v1'
SAFETY_CAPABILITY = dict(alerts=True, cases=True, publication='local_operator')
SAFETY_REVIEWED = {
    'primary': '94074d22b03e8ae3a0a61fac422291c0f229b348bce63a737f6ad56f80ccd1c8',
    'light': 'edfd08a8083ef3e4a435bef3b9d415ad8ed57be3e7112981083444b13061a70a',
}
COSINE_RECORD = 'q4t-profile-business-v46'
COSINE_FORMAT = 'q4t-eval-cosine-source-v1'
COSINE_REVIEWED = {
    'primary': '3a06296e569444482724f6b6415e19d61512f6ccdb5dae66a957e667ff330e92',
    'light': '0809999642ccd316c363a0a969ab045aeaf3c006594c7b22517cc4b9218c69af',
}
EVAL_RECORD = 'q4t-profile-business-v45'
EVAL_FORMAT = 'q4t-eval-source-v1'
EVAL_REVIEWED = {
    'primary': 'bd258166e4b8719408474b18aac3a5cf684722753f1bfc7e75709d53097cfe8e',
    'light': 'f8d8c92cab7612628fb1c105afdea20a9c081b77289e6a745367aded9db4a95b',
}
EVAL_CAPABILITY = dict(endpoint='/v1/evals',
    run_sources=['jsonl', 'completions', 'responses'],
    stored_sources=['stored_completions', 'responses'],
    graders=['string_check', 'text_similarity', 'python', 'label_model', 'score_model'],
    text_similarity=['fuzzy_match', 'bleu', 'gleu', 'rouge_1', 'rouge_2',
                     'rouge_3', 'rouge_4', 'rouge_5', 'rouge_l'],
    python_profile='local_default', row_checkpoints=True,
    pure_text_interruption_retries=1)
COSINE_EVALS = dict(EVAL_CAPABILITY,
    text_similarity=[*EVAL_CAPABILITY['text_similarity'], 'cosine'])
METEOR_EVALS = dict(COSINE_EVALS,
    text_similarity=[*EVAL_CAPABILITY['text_similarity'], 'meteor', 'cosine'])
COSINE_GRADER = dict(metric='cosine', model='qwen3-embedding-0.6b',
                     dimensions=1024, range=[0, 1], configured=True)
CHATKIT_RECORD = 'q4t-profile-business-v44'
CHATKIT_FORMAT = 'q4t-chatkit-source-v1'
CHATKIT_CAPABILITY = dict(selector='chatkit_beta=v1', client='/v1/chatkit',
    uploads='/v1/chatkit/attachments', grant_scope='project_user_workflow',
    client_tools='serial', thread_history=True, direct_document_upload=True)
FEEDBACK_CHATKIT = dict(CHATKIT_CAPABILITY, item_feedback=True, entity_mentions=True)
UPLOAD_CHATKIT = dict(FEEDBACK_CHATKIT, two_phase_document_upload=True)
CHATKIT_REVIEWED = {
    'primary': '62288a6044e8239640fa01bc9b1d52c43baaeac60a21fd890e5dabd5dfafd994',
    'light': '5ba0f338da57899669e6db3954c4124c98ffdc5d602819dee9921ecb92aa716c',
}
PREPARATION_RECORD = 'q4t-profile-business-v43'
PREPARATION_FORMAT = 'q4t-hosted-preparation-source-v1'
PREPARATION_CAPABILITY = dict(min_file_entries=2, max_files=50, source_pin_seconds=3600,
    worker_jobs=1, tenant_rotation=True, pending_attachment=True, session_failure_events=True,
    synchronous_fallback=True, core_readiness_dependency=False)
PREPARATION_REVIEWED = {
    'primary': 'fe389ddf35f8564241bdcc1e6884a221a765e33ce38ed0aedd6c387aa44ed6a1',
    'light': '042ec92da5594a72850730ef93f8b9c3c518a42b8c49de912a5281385fb3283f',
}
HOSTED_LIFECYCLE_RECORD = 'q4t-profile-business-v42'
HOSTED_LIFECYCLE_FORMAT = 'q4t-hosted-environment-lifecycle-source-v1'
HOSTED_LIFECYCLE_CAPABILITY = dict(prewarm_seconds=300, max_sessions=128,
    idle_seconds=1200, checkpoint_seconds=604800, live_event_retention=64,
    shared_files=True, turn_scoped_resume=True, pending_tool_keeps_active=True)
HOSTED_LIFECYCLE_REVIEWED = {
    'primary': '68481ee9e52c56bc0bd993214a52da4d1ebe654a28ab79ada05d7e30a5fbb1b0',
    'light': '99a16787d773574d10009467ccec6aaeeed421e365fbf22bde69a77d109e8ee7',
}
WEBHOOK_RECORD = 'q4t-profile-business-v41'
WEBHOOK_FORMAT = 'q4t-webhook-source-v1'
WEBHOOK_CAPABILITY = dict(policy_profile='q4t-webhook-delivery-policy-v1',
    endpoint='/v1/webhook_endpoints', event_types='/v1/webhook_event_types',
    delivery='at_least_once', max_retry_seconds=259200, https_only=True,
    request_override=False, core_readiness_dependency=False)
WEBHOOK_REVIEWED = {
    'primary': 'fb46c5e00a4d9a2d6cfa59e781ebe08fd8d684d8a51c3b9cb12474c5592726e9',
    'light': 'af923ee17d2cef62d6eefee8033a69b4d3fa4b6fb6400b49568c9cd6fa0b1fb1',
}
AUTOMATION_REVIEWED = {
    'primary': '39378d4ab67df2449d4f0953faf466c3be2d2a413b74c8abe57b79417aafa1d4',
    'light': 'ded04edfd79a1cd60454ae157a47c0f62ed02ddc81b85e563cfe8bc8b613cede',
}
AUTOMATION_RECORD = 'q4t-profile-business-v40'
AUTOMATION_FORMAT = 'q4t-agent-automation-source-v1'
AUTOMATION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/automations',
    schedules=['once','periodic'], targets=['new_session','existing_session'],
    manual_invocations=True, session_tool='manage_automation', session_scope='creator_only',
    tool_receipt='atomic_with_mutation')
TRANSLATOR_RECORD = 'q4t-profile-business-v39'
TRANSLATOR_FORMAT = 'q4t-translation-transport-source-v1'
TRANSLATOR_CAPABILITY = dict(client_secrets='/v1/realtime/translations/client_secrets',
    websocket='/v1/realtime/translations', calls='/v1/realtime/translations/calls',
    webrtc_output='remote_audio_track', data_channel='optional',
    requires=['native_asr','text_model','native_tts'])
TRANSLATOR_REVIEWED = {
    'primary': '2845a42aa6265ef5dc85a6c7bff236e6147067166bee588623a46795ed340a3e',
    'light': 'ad144a7a4324c52b3caea26fb7270ab8a4c831999215cfa192602303506ce478',
}
CODE_RECORD = 'q4t-profile-business-v38'
CODE_FORMAT = 'q4t-interpreter-workspace-source-v1'
CODE_CAPABILITY = dict(runtime_requirement='system_cpython_3_12', workspace='/mnt/data',
                       skill_mount='/mnt/skills', session_state='files', responses_citations=True,
                       assistant_output_files=True, file_limit=256, file_bytes=8388608,
                       workspace_bytes=16777216, network='disabled')
CODE_REVIEWED = {
    'primary': '9f9b7800cfc6e92efe0ce6009767c4dc4d6237cafdc526c31bf2db8fec4c2a44',
    'light': 'a841cfa9cacd791bd8425eebb5652c87a72ce7328d44f079c9a547d5f17a55f0',
}
SKILLS_RECORD = 'q4t-profile-business-v37'
SKILLS_FORMAT = 'q4t-skill-workspace-source-v1'
SKILLS_CAPABILITY = dict(endpoint='/v1/containers', skills='/v1/skills', skill_versions=True,
                         skill_mount='immutable_snapshot', file_contents=True)
SKILLS_REVIEWED = {
    'primary': 'dab417dc1d87af8a07f9636a2748e5ae5da0528ffdaa9ed02426c6d8a5c1874d',
    'light': '00985afbf62b8a7d2521003e6830763ff35073ef53e1fef862e27c84c8501e90',
}
ASSISTANTS_RECORD = 'q4t-profile-business-v36'
ASSISTANTS_FORMAT = 'q4t-assistants-run-source-v1'
ASSISTANTS_CAPABILITY = dict(selector='assistants=v2', endpoint='/v1/assistants', threads='/v1/threads',
    messages='/v1/threads/{thread_id}/messages', runs='/v1/threads/{thread_id}/runs',
    run_steps=True, submit_tool_outputs=True)
ASSISTANTS_REVIEWED = {
    'primary': '2016b070e7fb19a74b4bd421d27141642de84336dd1f0b6f09de948677628881',
    'light': 'a43a354cc03ef066bbd100712284a6dccdfe3b332b4f02b3dd9a879e41d8f58d',
}
TRANSLATION_RECORD = 'q4t-profile-business-v35'
TRANSLATION_FORMAT = 'q4t-translation-realtime-source-v1'
TRANSLATION_CAPABILITY = dict(endpoint='/v1/audio/translations', models=['whisper-1', 'qwen3-asr-0.6b'],
    backend='native_asr_and_chat', language='english', configured=True,
    formats=['json','text','verbose_json','srt','vtt'], subtitles_require='aligner-model-dir')
TRANSLATION_REVIEWED = {
    'primary': '75d0e41fe4033bca234cffebed3163869a3e2b5cebf1315ba639c0c36461ac96',
    'light': 'b7951dcde8dcbb1b3d75886640f23c218a5f77003d534c16fbd1b5e7b81d5b69',
}
SUBAGENT_RECORD = 'q4t-profile-business-v34'
SUBAGENT_FORMAT = 'q4t-managed-subagent-source-v1'
SUBAGENT_REVIEWED = {
    'primary': {
        'bc31006134f64f662919accea942c0d675d9ef9e5ad4fee50879eb3c1f66a292',
        'aefe830b6cafa11377e50e22e9fe4775bf42df2b9c9a6a459b24dd62befd0a73',
    },
    'light': {
        '6929d4f5200e4e59e3c4402cc531029c069214c5f581ab318affde4804b67822',
        'c325c73cc2584ec49f03a24e5a9ae09e2162ec49d9c84726cd70fab9faf5cd6c',
    },
}
SUBAGENT_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/subagents',
                           reads=['list','retrieve','items','turns','turn','turn_items'],
                           lifecycle=['create','interrupt','close','resume'], turn_events=True, session_memory=True)
MANAGED_TOOL_REVIEWED = {
    'primary': 'a861225ac8bf4e9216fc7670b152039340237ab83e8f002854c8f92d2cb58cc2',
    'light': '190809a136db0c5f0dec62fc808cd39df9db137470cf374da18640fe9801a13b',
}
ARTIFACT_CAPABILITY = dict(selector='agents=v1', source='/workspace/outputs/', immutable=True,
                           operations=['list', 'retrieve', 'content', 'delete'])
ARTIFACT_REVIEWED = {
    'primary': '9134b3c35009291ee215d9d807508c0171001eddc6aa8e01ec2f3111a0fb1f32',
    'light': '8ef5c5c0378b0c1ffafc830755607e43fbad9679e98bf6a11b66f5cfc09cb0d0',
}
WORKSPACE_CAPABILITY = dict(selector='agents=v1', prewarm_seconds=300,
                            idempotency_seconds=86400, workspace_files=True,
                            network=['disabled'], between_turn_writes=True)
WORKSPACE_REVIEWED = {
    'primary': {
        '405210114da0add45510e800d673dff2176f62fd8123e78a0a74ac80c33f65ac',
        '6e28e44b71aab96b044d30301e8b9aa2a36fe882031e4a35d350efa10bd3f846',
        'a4641474c6326b0fd6c40faed6ff8eea95fa1c4a70277dd2061cb9f95d430faa',
    },
    'light': {
        'b2051c6ba5674639ed476bf988487509fc9860aa4561bfc76c27eb12f4629f70',
        '994c7b2df26b9c275feec6ff859995ea50d92113cee096f764ac1cbacb90b9e5',
        '9d5abc3509687c8f38d690e97c111b80dfc5e07fd0fd47be0976608624182803',
    },
}
ENVIRONMENT_RECORD = 'q4t-profile-business-v30'
ENVIRONMENT_FORMAT = 'q4t-agent-environment-template-source-v1'
TEMPLATE_CAPABILITY = dict(selector='agents=v1', operations=['create','retrieve','update','list','delete'],
                           confidential_definition='sealed', default_network='disabled')
SESSION_TRACE_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions/{session_id}/traces',
                                format='otlp_json', anchor='root_turn_id', published_only=True)
ENVIRONMENT_REVIEWED = {
    'primary': '2dfe88998e517324a5cb71f02a3cc6d3135157caca3ae49d076755aa196a453f',
    'light': '608e130e4026322b69666b27572caee1c53d7558bf279771ee700c19f95f5125',
}
OAUTH_RECORD = 'q4t-profile-business-v29'
OAUTH_FORMAT = 'q4t-vault-oauth-profile-source-v1'
SESSION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents/sessions',
    environments=['none'], tool_types=['function'], stream=True,
    idempotent_input_events=True, cancel_turn=True, storage='authenticated_client')
SESSION_REVIEWED = {
    'primary': '3d6691759a569a94a7f9aaaa010f6eacc7bf10ca798a220b015139d73248e329',
    'light': '5c3e6a8a050d64e4da85319ea7bbb23aff80a80a72952c555f348599f151bafe',
}
VAULT_SESSION = dict(SESSION_CAPABILITY, tool_types=['function', 'mcp'],
    mcp=dict(connection_origin='service', transport='http',
             vault_auth=['static_bearer', 'mcp_oauth_access_token']))
OAUTH_SESSION = dict(VAULT_SESSION, mcp=dict(VAULT_SESSION['mcp'],
    vault_auth=[*VAULT_SESSION['mcp']['vault_auth'], 'mcp_oauth_refresh']))
WORKSPACE_SESSION = dict(OAUTH_SESSION, environments=['none', 'openai_hosted'])
MANAGED_TOOL_SESSION = dict(WORKSPACE_SESSION, tool_types=['function', 'mcp', 'tool_search', 'web_search'],
                            time_awareness='utc_at_each_model_step')
OAUTH_REVIEWED = {
    'primary': '1fb036ff722a21719546d1b042298cc91e489ecd9dc30ddcfb8602e546c683ef',
    'light': 'cc67e878110002a8be19fa7fbf61fe32d82b098731e570bf2008e8d0dfa54517',
}
VAULT_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/vaults',
    operations=['create', 'retrieve', 'update', 'list', 'delete'],
    storage='authenticated_client', lifecycle='archive',
    credentials=dict(auth=['mcp_oauth', 'static_bearer', 'environment_variable'],
                     write_only=True, rotation=True, archive_cascade=True))
VAULT_REVIEWED = {
    'primary': '2849d28c117d197f0ceb0bc6f3a6a43d63f2a5f0173766f82eae0399bb75e939',
    'light': 'e76621fdd0353aee0007baf9d55ae55c6c256b121be9ec0194757bf7e95a9c69',
}
REGISTRATION_CAPABILITY = dict(selector='agents=v1', endpoint='/v1/agents',
    operations=['create','retrieve','update','list','delete'],
    storage='authenticated_client', retention='until_deleted', tool_list_max_bytes=3145728)
REGISTRATION_REVIEWED = {
    'primary': 'd957150126ea565ce9d23c4b8707100cae80f95fd1b4ab5c81d5021e916565e1',
    'light': '2e68838e87b3fe8fb4f0b578269dac10ccb9fc000a34b490a57562613bff0736',
}

REFRESH_REVIEWED = {
    'primary': 'a759ecb078ce7791679061f24560ec4a0c9622e47732ab7dd675c97fdf4fe49f',
    'light': '5265a9815da252b78560b391e1cb2b441ae35e9c8e2cbc89d450e6e1a0e99dd5',
}
AGENT_REVIEWED = {
    'primary': 'fa7353fa12ef7aef3514e25cb97ecff0bffa49e260267296ceb4ff6ee179cbad',
    'light': 'de3a519c4411138d02a6d863704d1c8142a5bb0334257ad35b0a99e81f692f06',
}
REUSE_FORMAT = {RECORD_FORMAT: FORMAT, BETA_RECORD_FORMAT: BETA_FORMAT,
                AGENT_RECORD_FORMAT: AGENT_FORMAT, REGISTRATION_RECORD: REGISTRATION_FORMAT,
                SESSION_RECORD: SESSION_FORMAT, VAULT_RECORD: VAULT_FORMAT,
                OAUTH_RECORD: OAUTH_FORMAT, ENVIRONMENT_RECORD: ENVIRONMENT_FORMAT,
                WORKSPACE_RECORD: WORKSPACE_FORMAT, ARTIFACT_RECORD: ARTIFACT_FORMAT,
                MANAGED_TOOL_RECORD: MANAGED_TOOL_FORMAT, SUBAGENT_RECORD: SUBAGENT_FORMAT,
                TRANSLATION_RECORD: TRANSLATION_FORMAT, ASSISTANTS_RECORD: ASSISTANTS_FORMAT,
                SKILLS_RECORD: SKILLS_FORMAT, CODE_RECORD: CODE_FORMAT,
                TRANSLATOR_RECORD: TRANSLATOR_FORMAT, AUTOMATION_RECORD: AUTOMATION_FORMAT,
                WEBHOOK_RECORD: WEBHOOK_FORMAT, HOSTED_LIFECYCLE_RECORD: HOSTED_LIFECYCLE_FORMAT,
                PREPARATION_RECORD: PREPARATION_FORMAT, CHATKIT_RECORD: CHATKIT_FORMAT,
                EVAL_RECORD: EVAL_FORMAT, COSINE_RECORD: COSINE_FORMAT,
                SAFETY_RECORD: SAFETY_FORMAT, HISTORY_RECORD: HISTORY_FORMAT,
                METEOR_RECORD: METEOR_FORMAT, FEEDBACK_RECORD: FEEDBACK_FORMAT,
                UPLOAD_RECORD: UPLOAD_FORMAT, PAGINATION_RECORD: PAGINATION_FORMAT,
                COMPOSER_RECORD: COMPOSER_FORMAT, CHARACTER_RECORD: CHARACTER_FORMAT, TRACK_RECORD: TRACK_FORMAT,
                TYPED_HISTORY_RECORD: TYPED_HISTORY_FORMAT,
                STORED_REFUSAL_RECORD: STORED_REFUSAL_FORMAT,
                CHATKIT_RETRY_RECORD: CHATKIT_RETRY_FORMAT,
                CHATKIT_ACTIONS_RECORD: CHATKIT_ACTIONS_FORMAT,
                CHATKIT_BROWSER_RECORD: CHATKIT_BROWSER_FORMAT,
                CHATKIT_GENERATION_RECORD: CHATKIT_GENERATION_FORMAT,
                CHATKIT_WORKFLOW_RECORD: CHATKIT_WORKFLOW_FORMAT,
                CHATKIT_IMAGE_RECORD: CHATKIT_IMAGE_FORMAT,
                CHATKIT_WIDGET_RECORD: CHATKIT_WIDGET_FORMAT,
                SESSION_CONTROL_RECORD: SESSION_CONTROL_FORMAT,
                MEDIA_HANDOFF_RECORD: MEDIA_HANDOFF_FORMAT,
                VISUAL_ANSWER_RECORD: VISUAL_ANSWER_FORMAT, FRAME_RECORD: FRAME_FORMAT,
                EVAL_CANCEL_RECORD: EVAL_CANCEL_FORMAT, RESPONSE_FAILURE_RECORD: RESPONSE_FAILURE_FORMAT,
                VIDEO_CLIP_RECORD: VIDEO_CLIP_FORMAT, REGION_RECORD: REGION_FORMAT,
                PROGRAM_SDK_RECORD: PROGRAM_SDK_FORMAT,
                COMPACT_MEDIA_RECORD: COMPACT_MEDIA_FORMAT,
                COMPACT_WINDOW_RECORD: COMPACT_WINDOW_FORMAT,
                MCP_ERROR_RECORD: MCP_ERROR_FORMAT,
                CACHE_RETENTION_RECORD: CACHE_RETENTION_FORMAT,
                LAYER_RECORD: LAYER_FORMAT,
                INTERRUPTED_RETRY_RECORD: INTERRUPTED_RETRY_FORMAT,
                GRID_RECORD: GRID_FORMAT,
                FORK_RECORD: FORK_FORMAT,
                CLIENT_SHELL_RECORD: CLIENT_SHELL_FORMAT,
                PATCH_RECORD: PATCH_FORMAT,
                LOCAL_SHELL_RECORD: LOCAL_SHELL_FORMAT,
                APPROVAL_IDS_RECORD: APPROVAL_IDS_FORMAT,
                COMPACT_RECORD: COMPACT_FORMAT,
                PARTS_RECORD: PARTS_FORMAT,
                NULL_HISTORY_RECORD: NULL_HISTORY_FORMAT,
                TOOL_MAP_RECORD: TOOL_MAP_FORMAT}
# Shared by the qualification writer and the managed acceptance reader.
ACCEPTANCE_LABELS = {
    RECORD_FORMAT: 'ACCEPT_LIVE_PRIMARY_LIGHT',
    BETA_RECORD_FORMAT: 'ACCEPT_RESPONSES_BETA_PRIMARY_LIGHT',
    AGENT_RECORD_FORMAT: 'ACCEPT_HOSTED_AGENTS_PRIMARY_LIGHT',
    REGISTRATION_RECORD: 'ACCEPT_AGENT_REGISTRATION_PRIMARY_LIGHT',
    SESSION_RECORD: 'ACCEPT_AGENT_SESSIONS_PRIMARY_LIGHT',
    VAULT_RECORD: 'ACCEPT_AGENT_VAULTS_PRIMARY_LIGHT',
    OAUTH_RECORD: 'ACCEPT_VAULT_OAUTH_PRIMARY_LIGHT',
    ENVIRONMENT_RECORD: 'ACCEPT_ENVIRONMENT_TEMPLATES_PRIMARY_LIGHT',
    WORKSPACE_RECORD: 'ACCEPT_HOSTED_WORKSPACES_PRIMARY_LIGHT',
    ARTIFACT_RECORD: 'ACCEPT_SESSION_ARTIFACTS_PRIMARY_LIGHT',
    MANAGED_TOOL_RECORD: 'ACCEPT_MANAGED_SESSION_TOOLS_PRIMARY_LIGHT',
    SUBAGENT_RECORD: 'ACCEPT_SUBAGENT_PRIMARY_LIGHT',
    TRANSLATION_RECORD: 'ACCEPT_TRANSLATION_REALTIME_PRIMARY_LIGHT',
    ASSISTANTS_RECORD: 'ACCEPT_ASSISTANTS_PRIMARY_LIGHT',
    SKILLS_RECORD: 'ACCEPT_SKILLS_PRIMARY_LIGHT',
    CODE_RECORD: 'ACCEPT_INTERPRETER_PRIMARY_LIGHT',
    TRANSLATOR_RECORD: 'ACCEPT_TRANSLATION_TRANSPORTS_PRIMARY_LIGHT',
    AUTOMATION_RECORD: 'ACCEPT_AUTOMATION_PRIMARY_LIGHT',
    WEBHOOK_RECORD: 'ACCEPT_WEBHOOK_PRIMARY_LIGHT',
    HOSTED_LIFECYCLE_RECORD: 'ACCEPT_HOSTED_ENVIRONMENT_LIFECYCLE_PRIMARY_LIGHT',
    PREPARATION_RECORD: 'ACCEPT_HOSTED_PREPARATION_PRIMARY_LIGHT',
    RESPONSE_FAILURE_RECORD: 'ACCEPT_RESPONSE_FAILURE_PRIMARY_LIGHT',
    VIDEO_CLIP_RECORD: 'ACCEPT_VIDEO_CLIP_PRIMARY_LIGHT',
    PROGRAM_SDK_RECORD: 'ACCEPT_PROGRAM_SDK_PRIMARY_LIGHT',
    COMPACT_MEDIA_RECORD: 'ACCEPT_COMPACT_MEDIA_PRIMARY_LIGHT',
    COMPACT_WINDOW_RECORD: 'ACCEPT_COMPACT_WINDOW_PRIMARY_LIGHT',
    MCP_ERROR_RECORD: 'ACCEPT_MCP_ERROR_READERS_PRIMARY_LIGHT',
    CACHE_RETENTION_RECORD: 'ACCEPT_RESPONSE_CACHE_RETENTION_PRIMARY_LIGHT',
    LAYER_RECORD: 'ACCEPT_VIDEO_LAYER_PRIMARY_LIGHT',
    INTERRUPTED_RETRY_RECORD: 'ACCEPT_CHATKIT_INTERRUPTED_RETRY_PRIMARY_LIGHT',
    GRID_RECORD: 'ACCEPT_VIDEO_TRACK_GRID_PRIMARY_LIGHT',
    FORK_RECORD: 'ACCEPT_PROGRAM_FORK_PRIMARY_LIGHT',
    CLIENT_SHELL_RECORD: 'ACCEPT_CLIENT_SHELL_PRIMARY_LIGHT',
    PATCH_RECORD: 'ACCEPT_CLIENT_PATCH_PRIMARY_LIGHT',
    LOCAL_SHELL_RECORD: 'ACCEPT_LOCAL_SHELL_PRIMARY_LIGHT',
    APPROVAL_IDS_RECORD: 'ACCEPT_MCP_APPROVAL_IDENTIFIERS_PRIMARY_LIGHT',
    COMPACT_RECORD: 'ACCEPT_CONVERSATION_COMPACTION_PRIMARY_LIGHT',
    PARTS_RECORD: 'ACCEPT_STORED_CONTENT_PARTS_PRIMARY_LIGHT',
    NULL_HISTORY_RECORD: 'ACCEPT_NULL_ASSISTANT_HISTORY_PRIMARY_LIGHT',
    TOOL_MAP_RECORD: 'ACCEPT_TOOL_MAP_PRIMARY_LIGHT',
    REGION_RECORD: 'ACCEPT_QUICKDRAW_REGION_PRIMARY_LIGHT',
    EVAL_CANCEL_RECORD: 'ACCEPT_EVAL_CANCEL_PRIMARY_LIGHT',
    FRAME_RECORD: 'ACCEPT_VISUAL_FRAME_PRIMARY_LIGHT',
    VISUAL_ANSWER_RECORD: 'ACCEPT_VISUAL_ANSWER_PRIMARY_LIGHT',
    MEDIA_HANDOFF_RECORD: 'ACCEPT_RESPONSE_MEDIA_PRIMARY_LIGHT',
    SESSION_CONTROL_RECORD: 'ACCEPT_AGENT_SESSION_CONTROL_PRIMARY_LIGHT',
    CHATKIT_WIDGET_RECORD: 'ACCEPT_CHATKIT_WIDGET_PRIMARY_LIGHT',
    CHATKIT_IMAGE_RECORD: 'ACCEPT_CHATKIT_IMAGE_PRIMARY_LIGHT',
    CHATKIT_WORKFLOW_RECORD: 'ACCEPT_CHATKIT_WORKFLOW_PRIMARY_LIGHT',
    CHATKIT_GENERATION_RECORD: 'ACCEPT_CHATKIT_GENERATION_PRIMARY_LIGHT',
    CHATKIT_BROWSER_RECORD: 'ACCEPT_CHATKIT_BROWSER_PRIMARY_LIGHT',
    CHATKIT_ACTIONS_RECORD: 'ACCEPT_CHATKIT_ACTIONS_PRIMARY_LIGHT',
    CHATKIT_RETRY_RECORD: 'ACCEPT_CHATKIT_RETRY_PRIMARY_LIGHT',
    STORED_REFUSAL_RECORD: 'ACCEPT_STORED_REFUSAL_PRIMARY_LIGHT',
    TYPED_HISTORY_RECORD: 'ACCEPT_TYPED_HISTORY_PRIMARY_LIGHT',
    TRACK_RECORD: 'ACCEPT_VIDEO_TRACK_PRIMARY_LIGHT',
    CHARACTER_RECORD: 'ACCEPT_VIDEO_CHARACTER_PRIMARY_LIGHT',
    COMPOSER_RECORD: 'ACCEPT_CHATKIT_DICTATION_PRIMARY_LIGHT',
    PAGINATION_RECORD: 'ACCEPT_ASSISTANT_PAGINATION_PRIMARY_LIGHT',
    UPLOAD_RECORD: 'ACCEPT_CHATKIT_UPLOAD_PRIMARY_LIGHT',
    FEEDBACK_RECORD: 'ACCEPT_CHATKIT_FEEDBACK_PRIMARY_LIGHT',
    METEOR_RECORD: 'ACCEPT_EVAL_METEOR_PRIMARY_LIGHT',
    HISTORY_RECORD: 'ACCEPT_HISTORY_ARGUMENTS_PRIMARY_LIGHT',
    SAFETY_RECORD: 'ACCEPT_PROJECT_SAFETY_PRIMARY_LIGHT',
    COSINE_RECORD: 'ACCEPT_EVAL_COSINE_PRIMARY_LIGHT',
    EVAL_RECORD: 'ACCEPT_EVAL_PRIMARY_LIGHT',
    CHATKIT_RECORD: 'ACCEPT_CHATKIT_PRIMARY_LIGHT',
}

# The registration migration packed the original duplex transcript. All
# successors inherit it while the first three Live formats retained raw text.
PACKED_DUPLEX_RECORDS = REUSE_FORMAT.keys() - {
    RECORD_FORMAT, BETA_RECORD_FORMAT, AGENT_RECORD_FORMAT,
}
# These formats keep the compressed predecessor history in a separate field.
# Encoding and reconstruction must always select the same envelope.
SEPARATE_HISTORY_RECORDS = (
    COSINE_RECORD, SAFETY_RECORD, HISTORY_RECORD, METEOR_RECORD, FEEDBACK_RECORD,
    UPLOAD_RECORD, PAGINATION_RECORD, COMPOSER_RECORD, CHARACTER_RECORD,
    TRACK_RECORD, TYPED_HISTORY_RECORD, STORED_REFUSAL_RECORD, CHATKIT_RETRY_RECORD,
    CHATKIT_ACTIONS_RECORD, CHATKIT_BROWSER_RECORD, CHATKIT_GENERATION_RECORD, CHATKIT_WORKFLOW_RECORD,
    CHATKIT_IMAGE_RECORD, CHATKIT_WIDGET_RECORD, SESSION_CONTROL_RECORD, MEDIA_HANDOFF_RECORD,
    VISUAL_ANSWER_RECORD, FRAME_RECORD, EVAL_CANCEL_RECORD, RESPONSE_FAILURE_RECORD, VIDEO_CLIP_RECORD, REGION_RECORD, TOOL_MAP_RECORD, PROGRAM_SDK_RECORD, COMPACT_MEDIA_RECORD, COMPACT_WINDOW_RECORD, MCP_ERROR_RECORD, CACHE_RETENTION_RECORD, LAYER_RECORD, INTERRUPTED_RETRY_RECORD, GRID_RECORD, FORK_RECORD, CLIENT_SHELL_RECORD, PATCH_RECORD, LOCAL_SHELL_RECORD, APPROVAL_IDS_RECORD, COMPACT_RECORD, PARTS_RECORD, NULL_HISTORY_RECORD,
)
BETA_REVIEWED = {
    'primary': 'e2178b021652317a2d67189b73982edc3a7a1b70c4c2716929a448a5d579f07a',
    'light': '19299d0067ef28685380c4bf080526c2463e5f083ae9868c98bf6c869a92cd0c',
}
METADATA = ('format', 'configuration', 'startup_memory', 'environment', 'reused_business')
# Receipts are canonical profile record bytes including their terminating newline.
REVIEWED = {
    'primary': {
        'previous': '6255bf549874162c3e846206528a706af463f4c5facfffe0d087918e349858a5',
        'selected': 'd10054a6a8dae78f324ec37f510e32761c00568f0957d16927bbea583a587a0f'
    },
    'light': {
        'previous': '5da10f635a7ea8e289de5f114017237fa51dabcb90abadba433c7d5ef0337bf6',
        'selected': '16337b914b6b7a7b4bb476b0185b7dd9340be77eae44253d0c40f52e612aef98'
    }
}


# The SDK ran through this native ingress. The acceptance hash is carried in
# both reviewed profile records. An engine capability cannot qualify a gateway.
ACCEPTED_INGRESS = {
    '07d7d6f4f2d3b9bfb63a1b678e9ee9a08541a1e79c9e8ecaab5f3238033e1b7d':
        'd886ac03ea8432eb80a52b271bf6810f2abc5a9d9305acdbc8c2a0d6340e7684',
    'b9b93fbb4ba2d1a1ad7a81b5003807f0a05ed2ca937c41a4821a3189448b6324':
        'd886ac03ea8432eb80a52b271bf6810f2abc5a9d9305acdbc8c2a0d6340e7684',
    '1cfeabca03225efefc7031ac907b67cf4daa6a8f1c9434b0609947d93d5c1a2e':
        'd886ac03ea8432eb80a52b271bf6810f2abc5a9d9305acdbc8c2a0d6340e7684',
    '49aba35cbfe9dc1062385384f2a8ce4734b5c4c5ae3f00e96b01178afec2a95b':
        'a93a47261ea7bd97cf1ffbd119cc374ee11d07740b6ae1c8257cfd8c126ad780',
    'c4567531c43c2b9c531d9cc700d4eeaeeab1348508ede91231e670e85a9e24bb':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '2290b024c2271c4d85716086b1504ee9eb1f425e60199a8b619181e809fdc9ec':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '0fae97bc1684b6ce71276e8f138b59cc6c40038bb2068f81a59282ab0ca1fa95':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '03bef9da9c1653fb39260abd4915e3afee4e4979264b0bc9653d1dfa1cf86592':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '8a9a92c7d7f81e5ed459fbae0bd236f8be3c76ae5212dfcf3469439a0e86d093':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    'e36b2d9270d2428765fed0768f669f3aa8bacc998230c865682f8a2d663387c3':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '550175d67a5afaaf7410b829ceddbf67732303847434024f340e6519340ee32a':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    'c4137354c407b45b33b7e6eb828a09302fe891ed07bb61ae40f61f163bf02724':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '4ab076ee4cf44ced53cf28f244166e96fe9ca77f72d04b4ec20edc588b0d5ced':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '5e761f97c2a67eeeaba19c3584f2e56ec4fc2dab374296d2f0350418df1fe711':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '2ba26d2cff45edced5130ac36e187b8d0892a284b8560ef821abe9372c82ea42':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '57bc33891ce68177931aa2ebf5c38ff1b9a18a4170b88075e2c53538ea50862c':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    'de3b5147ce049e4ca7e1e0ef139bfac29c9d880f5efd13dc4d8077c6ea73f3c0':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '84d333f0df9e2218388af296381fa448a3af9ddd6dad13508d881f49323e685e':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    'c2ebf8c338cc5f3993b165d8d8df26e9f6cf1c10c86f690c59f5327c78adb10f':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    'd1e76f7b0fcebde0b097423e8344c54a551eb1fae054544968ceb8df429bde17':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '884368197f40b727d1ad3897703d3a7f0a344c11f79617f394d1f6f02bc0f09f':
        '65f6e9873b92fc68b2911a41ae6f6ab078975d9142e9c54a01da5e1b26632d61',
    '8390d912b9bd44d2b430477236b9dbf20c6ddd1b5720db83e0e398c94079e672':
        '7f8dae56d2c441509741fe829ca7b3f82eb5f966dfa326aed427040f978e79cb',
    '152389cb1dabb141407f22525a5425ec82c4d54cb4acbd0bb5d3e9ba769a4a27':
        '7f8dae56d2c441509741fe829ca7b3f82eb5f966dfa326aed427040f978e79cb',
    '9917de3f728c61679fa9a45242468a10f02dbc73f1d0405d689cf8b68888a179':
        '7f8dae56d2c441509741fe829ca7b3f82eb5f966dfa326aed427040f978e79cb',
    '5b0f2cce0ac0fdeeac3d897b029f0c3aa6d7b907adec6407054e32c4628345f9':
        '666d8c5fcef03dfa15ca0ece7c8e952c4f542449d817d357fd0f98adcbf049b8',
    'ba548f84528f29b1b7e755ae1895f79926de8fca3352766702ef4084d44241f9':
        '666d8c5fcef03dfa15ca0ece7c8e952c4f542449d817d357fd0f98adcbf049b8',
    'f564dd1996222e4e60c473c1c253d72fb91696a3f566a43e8d3b6b8a25abba4b':
        '666d8c5fcef03dfa15ca0ece7c8e952c4f542449d817d357fd0f98adcbf049b8',
    '1d6734458a00a8a250270f5dffb914be0ab8de44555f7adab33880ec7b9f1123':
        '666d8c5fcef03dfa15ca0ece7c8e952c4f542449d817d357fd0f98adcbf049b8',
    'bb44d2fe8f26cdf0f8cf3c389e7d13b890b915b25a27e847c306e197d2c0c544':
        '666d8c5fcef03dfa15ca0ece7c8e952c4f542449d817d357fd0f98adcbf049b8',
    'b4f72adfc87c8f0d10df61b53cee9f122e9bc776d1b068343736407d4da67b7c':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    'ff1c5e7deedba25da1f98ae9b9f77bb7f2c8e0377c394948857a8510b49292b5':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    'c988d41f894582cf797cef7a58558cf82e4d3ccdb9ce2d16e5ed64b49e0cb24c':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    'f8cf174d68360146f7671d4aba24bafe54744ce1cea1cbfcf8b4fe8cffa70ecd':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    '9e55d1861f434b4d0c3d7ef37ed25188625a2b659a71586bed396337a4b83ba4':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    '5943cfda1ccef482417efc40e33f7cac49d727eec8e3347aec2a36a1c443c940':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    'e92ebe71f86d0d9e993650fef5f66e2ce9b0c3e59227f191b38f563535384b89':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    '41fe013b5c5bf84c6c9ddb0434e81f6f6d403172beb9ffeda8cf734cab89cdaa':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    '09f823b3e0a2cdb7ed841f66085f1eb3875c050ee80151f4352f2d1782079533':
        '3eaf3d9781cbd0ea6b7f212e5f6d2682b49a0c3db3a128fa8efd6764b924de87',
    '608fc278b963de946a12f658cb749b330686de151472292bf5d23d6f36628502':
        '3fc018c2dcfd69433ddff22ac2b4e99cbba525e72f26d0e56433967f86d92d4a',
    '6b14a6aebbb7e7ab254ebf230440b3bcb79f09dcab616df314bd781c8722816e':
        '47efe94a638420a7e5cd24a6505597e8096545c3279f40ae7dd6298e1efcf8be',
    '3b8c313babf2e7ff4e5c8988c55c6269fcf7f6ec2bf6ca15d797260822255ccd':
        '47efe94a638420a7e5cd24a6505597e8096545c3279f40ae7dd6298e1efcf8be',
    'ff4424db6d0bda8b460b607fb83ac6aba9ce6862b578a2dca8b252b96539f9e9':
        '842072a374fdc040504d96c2a16746772621860237478a4088b1de3b2c10ff65',
    'accd702f7be79eb0f5812abb98bf21caf9a821319f1f1ca825700196d1432eb3':
        '842072a374fdc040504d96c2a16746772621860237478a4088b1de3b2c10ff65',
    '6c06d216990981de14ee2c41023aadc89440610e73e3c7f0ee5cb43dbf4996fd':
        '842072a374fdc040504d96c2a16746772621860237478a4088b1de3b2c10ff65',
    '9b4414892237b10b1e4d6f1720c7636c5de4c26ac97ace169ca8762121e196b3':
        '842072a374fdc040504d96c2a16746772621860237478a4088b1de3b2c10ff65',
    '5c35c82b1e0b5aa927d3b6e3281f5341f5620fdcc6c9c0c228ee1493a726cce4':
        '1efb565263062c11b682bab1905424da4fdb84fc902c87c67bb3a60e2e57c137',
    'dcb959dc77dcbd23b96df4c56d5d3c6a86386a342f4dd8de4cc244e9bb265c0e':
        '69a8c51d99aaaf17574cef5e5d688c808925cf5eeb69d2642125d22480e7174e',
    'b65198bc23c0b545f11ed565601fa9942c8a1ca960d0613a385de251168073e4':
        '69a8c51d99aaaf17574cef5e5d688c808925cf5eeb69d2642125d22480e7174e',
    'b4250176a3bdf3c94fab37921aadf211e9c29edef1da7e83965f8e0aaf8f049e':
        'a1d1070beabbe1b26dce51e4261143ee966ac0ef1a8b9c64e2d745f7af5b9c32',
    'cd0d8ac581bde958758caf1b8dde20c470b3606b5562d73581cce376483d4c41':
        'ac71057cdb01b81bb3f5ba5509fe1f7e16821ac7e385676977bfe7c8be7b9f46',
    '3280676f47c89ff879f88386e7ff954fc91e583d873525e3571eb5c1c12aa237':
        'ac71057cdb01b81bb3f5ba5509fe1f7e16821ac7e385676977bfe7c8be7b9f46',
    '5c530a6c2de1a1af191645ac0b0c51ee1af38ceacfa36bf6f26fa21d5168abb5':
        'ac71057cdb01b81bb3f5ba5509fe1f7e16821ac7e385676977bfe7c8be7b9f46',
    '97196543bab99ad9654f62e9ff56d591580d951abfe54d9336c0b42ada918264':
        'f06eb62270ee8efa305cda7aa7ac6b60780ba790efdb1a545c6bf18861b9a06a',
    '315bf9fd8172b37e40c0ad7de0e9ae46a98db4f2c6d9914a59306032ff43aa44':
        '7b9770f3784c047bce10342b5a085c2b6ca3a231be285f7fae3580fffe60ec36',
    '5429ed3286976393ee139a6a848ed81c8981335c373789bcb0d4a96f059fc6f3':
        '8dcbff0906e72c0109a9b36febe529e92a9708d2c2b5cb8d0c934bc27f453572',
    '7c061333749eeeeb9084b3f020adbdca3703e81470a7d95ea8caf78b11003a7b':
        '8dcbff0906e72c0109a9b36febe529e92a9708d2c2b5cb8d0c934bc27f453572',
    '432ffbbea808303224305fdbc043548f7366d175dd8c8f7bd4657c9091aa43c8':
        'f4ef4776b8db25a9a303234f8497a5595ed6082e850d71ec179717c43a481e15',
    '92a0ac0c3d173820cdb36c1002ed4f46b0063fba3bb5727f655db05f7e827cfe':
        '458b028f02d936beb055a4ffe1d4c6a55bc7766082c039339d3c2b4b05bc6919',
    '82f102fbf86efb6c7a861ae3fb0243f64402988507d8ba4748251f5642d4e433':
        '9e72f5309977c0f7ae4f3e7c192b12029a8ba6a1d845da688fc6b140c66c8f87',
    '1de6946fe64e0d94905bf146bc242b483cb07e9e0a3a83d15b0ab01d94dd57dc':
        'f5ad763378beeec259b3df29aa14a2e39cf7906440a0b6fe5cafb9a61b9b9c37',
    '555452009732e11ab1859f4e4996b23c69669691396be2d943850b40cb55e4f9':
        'a201da20bee226df67d1c7c409c0431b587dfe4b648f09d71ae046d6d6ffc5dc',
    'a2e3011a39b2d9d2cf8d55dbf93e6119b98b06281d2470d5273c1e1f49bdc37c':
        'a201da20bee226df67d1c7c409c0431b587dfe4b648f09d71ae046d6d6ffc5dc',
    'a486fc871b0c8c4b3d46e85d1ad092815836bb0f6f38ad34ad8bc4225d670b70':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    'c4007692ba30e2a42930e73a56b74068fdaa460b869f02a597f86bbf19fe48df':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    '06022952c38de6d1cebc5a77ab9f254877a73c5eae3796f2259df662b911f658':
        '3e1b8020a0216da9db599b78b3c64ce1d454fd56019df62c6dc5ecfb567b62ed',
    '6665a8f7d995806d68572b8c141110538bb689d25c872979a02ddcda5bc8d367':
        '8afa45887db80173df1841d48f11c50a507b6828e861e555018648744a07acb5',
    '3d05f0cc1c99a70f16e4f96291d6e6234a680765facc386c93d59f72ce5a40be':
        '1e71f9b806763a77a7dce8923ed9585f3d678b2105acfbdf9c9fa98be6e16593',
    '26dab53af96b229e3b653e983828f7b4ef3979e4237a564c9518eeffc307dc33':
        '1e71f9b806763a77a7dce8923ed9585f3d678b2105acfbdf9c9fa98be6e16593',
    'f742f4a99411c3f60099d219e1c4959a08c06e9878d34ff9153de1b256a3105c':
        '96449e96cf5401726e47505e3a816dacd18ad2eeb8d4c09114eb54a24a8dddc8',
    '7014bde675707b1bc3a52765ebbacfe2455a6b0be61b1eee7d4a61c3cb36dd5f':
        '4ef57fba3d32d8a213471f7db499a4e097c82b120da4e2a08dd7878a10f71d3b',
    'da34734b772404e09ed8f7ac1c91f08683a712194c27c97a4c8e69f6501e8b88':
        '72e916a3571768d7b256c3c12ff8710333ae03933ebc8d713f9b3f1ff467eec3',
    '0c2bb7554043ff66e2d59113a666b5233ac7a2a52d1042f5c0f2f1d48417ba0b':
        '72e916a3571768d7b256c3c12ff8710333ae03933ebc8d713f9b3f1ff467eec3',
    '29c31bf9ef841fe729d2fc3085888153489d398e40c7508a64a726c5a431d019':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
    '1e744c78e3c8fc9600add6ac99c800ff58c69bb4a6acf1218104fc7bd85205d6':
        'b7a1967ba8540dc51ae7187577052bb1273d789d85ed8ab5a2d1cf5b02f21caf',
}


def verify_ingress(live, digest):
    expected = ACCEPTED_INGRESS.get(live['acceptance_sha256'])
    ledger.require(expected is not None and digest == expected,
                   'installed ingress differs from the qualified Live business route')


def record_hash(value):
    return hashlib.sha256(ledger.canonical(value) + b'\n').hexdigest()


def automation_session():
    return dict(MANAGED_TOOL_SESSION,
                tool_types=[*MANAGED_TOOL_SESSION['tool_types'], 'automations'])


def has_meteor(capabilities):
    evals = capabilities.get('evals')
    metrics = evals.get('text_similarity') if isinstance(evals, dict) else None
    return isinstance(metrics, list) and 'meteor' in metrics


def has_feedback(capabilities):
    chatkit = capabilities.get('chatkit')
    return isinstance(chatkit, dict) and bool({'item_feedback', 'entity_mentions'} & chatkit.keys())


def has_upload_reservations(capabilities):
    chatkit = capabilities.get('chatkit')
    return isinstance(chatkit, dict) and 'two_phase_document_upload' in chatkit


def composer_capability(capabilities):
    enabled = capabilities.get('transcriptions', {}).get('configured')
    ledger.require(type(enabled) is bool, 'ASR configuration is missing')
    return dict(UPLOAD_CHATKIT, input_transcribe=dict(operation='input.transcribe', configured=enabled))


def client_shell_capability(previous):
    items = list(previous['output_items'])
    ledger.require('shell' not in previous and 'shell_call' not in items
                   and items.count('apply_patch_call') == 1, 'invalid client shell predecessor')
    items.insert(items.index('apply_patch_call') + 1, 'shell_call')
    return dict(previous, output_items=items, shell={
        'executor': 'client', 'environments': ['local'],
        'automatic_execution': False, 'result_item': 'shell_call_output'})


def client_patch_capability(previous):
    items = list(previous['output_items'])
    ledger.require('apply_patch' not in previous and 'apply_patch_call' not in items
                   and items.count('local_shell_call') == 1, 'invalid patch predecessor')
    items.insert(items.index('local_shell_call') + 1, 'apply_patch_call')
    return dict(previous, output_items=items, apply_patch={
        'executor': 'client', 'automatic_execution': False,
        'operations': ['create_file', 'update_file', 'delete_file'],
        'result_item': 'apply_patch_call_output'})


def local_shell_capability(previous):
    items = list(previous['output_items'])
    ledger.require('local_shell' not in previous and 'local_shell_call' not in items
                   and items.count('custom_tool_call') == 1,
                   'expected an issued Responses declaration before local shell')
    items.insert(items.index('custom_tool_call') + 1, 'local_shell_call')
    return dict(previous, output_items=items, local_shell={
        'executor': 'client', 'automatic_execution': False, 'result_item': 'local_shell_call_output'})


def clipped_file_inputs(previous):
    native = previous['native_video']
    ledger.require('rectangular_clipping' not in native, 'predecessor already declares clipping')
    return dict(previous, native_video=dict(native, rectangular_clipping=CLIP_CAPABILITY))


def region_file_inputs(previous):
    native = previous['native_video']
    ledger.require(native.get('rectangular_clipping') == CLIP_CAPABILITY and
                   'region_clipping' not in native, 'expected the issued rectangle capability')
    region = dict(CLIP_CAPABILITY, encoding='quickdraw-inversion-points',
                  frozen_conversion='q4t.cpu_video_png.v7')
    return dict(previous, native_video=dict(native, region_clipping=region))


def track_grid_file_inputs(previous):
    native = previous['native_video']
    ledger.require(native.get('region_clipping', {}).get('frozen_conversion') == 'q4t.cpu_video_png.v7'
                   and 'track_display_size' not in native, 'expected the issued video region capability')
    return dict(previous, native_video=dict(native, track_display_size={
        'source': 'quicktime-tkhd-fixed16', 'fractional_sampling': 'pixel-center-bilinear16',
        'clips_optional': True, 'frozen_conversion': 'q4t.cpu_video_png.v8'}))


def layer_file_inputs(previous):
    native = previous['native_video']
    ledger.require(native.get('track_display_size', {}).get('frozen_conversion') == 'q4t.cpu_video_png.v8'
                   and 'active_track_composition' not in native, 'expected the issued track grid capability')
    return dict(previous, native_video=dict(native, active_track_composition={
        'graphics_mode': 'copy', 'layer_order': 'lower-in-front', 'alternate_group': 'lowest-enabled-id',
        'timeline': 'movie', 'background': 'black', 'frozen_conversion': 'q4t.cpu_video_png.v9'}))


def format_for(capabilities, evidence=None):
    if 'video_characters' in capabilities:
        prior = dict(capabilities)
        character = prior.pop('video_characters')
        ledger.require(ledger.canonical(character) == ledger.canonical(CHARACTER_CAPABILITY)
                       and CHARACTER_RECEIPT in (evidence or {})
                       and format_for(prior, evidence) == COMPOSER_RECORD,
                       'character qualification requires a verified reference and the issued dictation handoff')
        if PROGRAM_SDK_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != PROGRAM_SDK_RECEIPT}
            ledger.require(format_for(capabilities, previous) == COMPACT_MEDIA_RECORD,
                           'SDK reader qualification requires the issued compact media profile')
            return PROGRAM_SDK_RECORD
        if COMPACT_MEDIA_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != COMPACT_MEDIA_RECEIPT}
            ledger.require(format_for(capabilities, previous) == MCP_ERROR_RECORD,
                           'compact window qualification requires the issued MCP profile')
            return COMPACT_MEDIA_RECORD
        if COMPACT_WINDOW_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != COMPACT_WINDOW_RECEIPT}
            ledger.require(format_for(capabilities, previous) == MCP_ERROR_RECORD,
                           'compact window qualification requires the issued MCP profile')
            return COMPACT_WINDOW_RECORD
        if MCP_ERROR_RECEIPT in evidence:
            previous = {key: digest for key, digest in evidence.items() if key != MCP_ERROR_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CACHE_RETENTION_RECORD,
                           'MCP readers require the issued Responses cache profile')
            return MCP_ERROR_RECORD
        if CACHE_RETENTION_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != CACHE_RETENTION_RECEIPT}
            ledger.require(format_for(capabilities, previous) == LAYER_RECORD,
                           'cache retention qualification requires the issued video-layer profile')
            return CACHE_RETENTION_RECORD
        if LAYER_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != LAYER_RECEIPT}
            ledger.require(format_for(capabilities, previous) == INTERRUPTED_RETRY_RECORD,
                           'video layers require the issued interrupted retry profile')
            return LAYER_RECORD
        if INTERRUPTED_RETRY_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != INTERRUPTED_RETRY_RECEIPT}
            ledger.require(format_for(capabilities, previous) == GRID_RECORD,
                           'interrupted retry qualification requires the issued track-grid record')
            return INTERRUPTED_RETRY_RECORD
        if GRID_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != GRID_RECEIPT}
            ledger.require(format_for(capabilities, previous) == FORK_RECORD,
                           'track grid qualification requires the issued program fork record')
            return GRID_RECORD
        if FORK_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != FORK_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CLIENT_SHELL_RECORD,
                           'program forks require the released shell predecessor')
            return FORK_RECORD
        if CLIENT_SHELL_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != CLIENT_SHELL_RECEIPT}
            ledger.require(format_for(capabilities, previous) == PATCH_RECORD,
                           'client shell handoff requires the issued client patch predecessor')
            return CLIENT_SHELL_RECORD
        if PATCH_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != PATCH_RECEIPT}
            ledger.require(format_for(capabilities, previous) == LOCAL_SHELL_RECORD,
                           'patch handoff requires the issued shell predecessor')
            return PATCH_RECORD
        if LOCAL_SHELL_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != LOCAL_SHELL_RECEIPT}
            ledger.require(format_for(capabilities, previous) == APPROVAL_IDS_RECORD,
                           'shell handoff requires the qualified approval predecessor')
            return LOCAL_SHELL_RECORD
        if APPROVAL_IDS_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != APPROVAL_IDS_RECEIPT}
            ledger.require(format_for(capabilities, previous) == COMPACT_RECORD,
                           'approval handoff requires the accepted conversation predecessor')
            return APPROVAL_IDS_RECORD
        if COMPACT_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != COMPACT_RECEIPT}
            ledger.require(format_for(capabilities, previous) == PARTS_RECORD,
                           'compact conversation requires the accepted stored-history predecessor')
            return COMPACT_RECORD
        if PARTS_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != PARTS_RECEIPT}
            ledger.require(format_for(capabilities, previous) == NULL_HISTORY_RECORD,
                           'stored parts require their qualified Chat history predecessor')
            return PARTS_RECORD
        if NULL_HISTORY_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != NULL_HISTORY_RECEIPT}
            ledger.require(format_for(capabilities, previous) == TOOL_MAP_RECORD,
                           'nullable history requires its reviewed tool-capable predecessor')
            return NULL_HISTORY_RECORD
        if TOOL_MAP_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != TOOL_MAP_RECEIPT}
            ledger.require(format_for(capabilities, previous) == REGION_RECORD,
                           'tool dictionary qualification requires the issued region profile')
            return TOOL_MAP_RECORD
        if REGION_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != REGION_RECEIPT}
            ledger.require(format_for(capabilities, previous) == VIDEO_CLIP_RECORD,
                           'region qualification requires the issued rectangle handoff')
            return REGION_RECORD
        if VIDEO_CLIP_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != VIDEO_CLIP_RECEIPT}
            ledger.require(format_for(capabilities, previous) == RESPONSE_FAILURE_RECORD,
                           'clip qualification requires the issued Responses handoff')
            return VIDEO_CLIP_RECORD
        if RESPONSE_FAILURE_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != RESPONSE_FAILURE_RECEIPT}
            ledger.require(format_for(capabilities, previous) == EVAL_CANCEL_RECORD,
                           'failed response qualification requires the issued Evals handoff')
            return RESPONSE_FAILURE_RECORD
        if EVAL_CANCEL_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != EVAL_CANCEL_RECEIPT}
            ledger.require(format_for(capabilities, previous) == FRAME_RECORD,
                           'Evals cancellation requires the preceding managed frame qualification')
            return EVAL_CANCEL_RECORD
        if FRAME_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != FRAME_RECEIPT}
            ledger.require(format_for(capabilities, previous) == VISUAL_ANSWER_RECORD,
                           'frame qualification requires the issued visual answer handoff')
            return FRAME_RECORD
        if VISUAL_ANSWER_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != VISUAL_ANSWER_RECEIPT}
            ledger.require(format_for(capabilities, previous) == MEDIA_HANDOFF_RECORD,
                           'visual qualification requires the issued image and speech handoff')
            return VISUAL_ANSWER_RECORD
        if MEDIA_HANDOFF_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != MEDIA_HANDOFF_RECEIPT}
            ledger.require(format_for(capabilities, previous) == SESSION_CONTROL_RECORD,
                           'missing issued session controls for image and speech handoff')
            return MEDIA_HANDOFF_RECORD
        if SESSION_CONTROL_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != SESSION_CONTROL_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CHATKIT_WIDGET_RECORD,
                           'missing issued widget qualification for session controls')
            return SESSION_CONTROL_RECORD
        if CHATKIT_WIDGET_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != CHATKIT_WIDGET_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CHATKIT_IMAGE_RECORD,
                           'missing issued image qualification for widget handoff')
            return CHATKIT_WIDGET_RECORD
        if CHATKIT_IMAGE_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != CHATKIT_IMAGE_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CHATKIT_WORKFLOW_RECORD,
                           'missing issued public workflow handoff for image history')
            return CHATKIT_IMAGE_RECORD
        if CHATKIT_WORKFLOW_RECEIPT in evidence:
            previous = {k: v for k, v in evidence.items() if k != CHATKIT_WORKFLOW_RECEIPT}
            ledger.require(format_for(capabilities, previous) == CHATKIT_GENERATION_RECORD,
                           'missing issued generation handoff for public summaries')
            return CHATKIT_WORKFLOW_RECORD
        if CHATKIT_GENERATION_RECEIPT in evidence:
            ledger.require(all(item in evidence for item in (
                CHATKIT_BROWSER_RECEIPT, CHATKIT_ACTIONS_RECEIPT,
                CHATKIT_RETRY_RECEIPT, STORED_REFUSAL_RECEIPT, TYPED_HISTORY_RECEIPT)),
                'missing predecessor browser generation or tool history evidence')
            return CHATKIT_GENERATION_RECORD
        if CHATKIT_BROWSER_RECEIPT in evidence:
            ledger.require(CHATKIT_ACTIONS_RECEIPT in evidence and CHATKIT_RETRY_RECEIPT in evidence
                           and STORED_REFUSAL_RECEIPT in evidence and TYPED_HISTORY_RECEIPT in evidence,
                           'missing issued form and action qualification')
            return CHATKIT_BROWSER_RECORD
        if CHATKIT_ACTIONS_RECEIPT in evidence:
            ledger.require(CHATKIT_RETRY_RECEIPT in evidence and STORED_REFUSAL_RECEIPT in evidence
                           and TYPED_HISTORY_RECEIPT in evidence, 'missing issued ChatKit retry history')
            return CHATKIT_ACTIONS_RECORD
        if CHATKIT_RETRY_RECEIPT in evidence:
            ledger.require(STORED_REFUSAL_RECEIPT in evidence and TYPED_HISTORY_RECEIPT in evidence,
                           'missing prior stored-message or tool-history evidence')
            return CHATKIT_RETRY_RECORD
        if STORED_REFUSAL_RECEIPT in evidence:
            ledger.require(TYPED_HISTORY_RECEIPT in evidence and TRACK_RECEIPT in evidence,
                           'missing issued typed tool history or selected track evidence')
            return STORED_REFUSAL_RECORD
        if TYPED_HISTORY_RECEIPT in evidence:
            ledger.require(TRACK_RECEIPT in evidence, 'missing issued video track handoff')
            return TYPED_HISTORY_RECORD
        return TRACK_RECORD if TRACK_RECEIPT in evidence else CHARACTER_RECORD
    if 'input_transcribe' in capabilities.get('chatkit', {}):
        prior = dict(capabilities, chatkit=UPLOAD_CHATKIT)
        ledger.require(ledger.canonical(capabilities['chatkit']) == ledger.canonical(composer_capability(capabilities))
                       and PAGINATION_RECEIPT in (evidence or {})
                       and COMPOSER_RECEIPT in evidence and format_for(prior) == UPLOAD_RECORD,
                       'dictation qualification requires its own handoff and the issued pagination evidence')
        return COMPOSER_RECORD
    # The public capability set is unchanged by the cursor repair. Select this
    # record using the observed SDK handoff; validation binds both profiles.
    if PAGINATION_RECEIPT in (evidence or {}):
        ledger.require(format_for(capabilities) == UPLOAD_RECORD,
                       'pagination verification requires the issued upload profile')
        return PAGINATION_RECORD
    if has_upload_reservations(capabilities):
        prior = dict(capabilities, chatkit=FEEDBACK_CHATKIT)
        ledger.require(ledger.canonical(capabilities['chatkit']) == ledger.canonical(UPLOAD_CHATKIT)
                       and format_for(prior) == FEEDBACK_RECORD,
                       'attachment reservations require the complete issued ChatKit contract')
        return UPLOAD_RECORD
    if has_feedback(capabilities):
        ledger.require(ledger.canonical(capabilities['chatkit']) == ledger.canonical(FEEDBACK_CHATKIT)
                       and format_for(dict(capabilities, chatkit=CHATKIT_CAPABILITY)) == METEOR_RECORD,
                       'browser feedback requires the issued evaluation and ChatKit contract')
        return FEEDBACK_RECORD
    if has_meteor(capabilities):
        ledger.require(ledger.canonical(capabilities['evals']) == ledger.canonical(METEOR_EVALS)
                       and format_for(dict(capabilities, evals=COSINE_EVALS)) == HISTORY_RECORD,
                       'METEOR requires the issued metrics and complete tool history')
        return METEOR_RECORD
    tools = capabilities.get('tools', {})
    history = tools.get('history', {})
    if 'non_mapping_text' in history or 'unidentified_arguments' in history:
        prior = dict(capabilities, tools=dict(tools, history=ISSUED_TOOL_HISTORY))
        ledger.require(ledger.canonical(history) == ledger.canonical(REPAIRED_TOOL_HISTORY)
                       and format_for(prior) == SAFETY_RECORD,
                       'tool history requires its complete repair contract and issued predecessor')
        return HISTORY_RECORD
    if 'project_safety' in capabilities:
        prior = dict(capabilities)
        safety = prior.pop('project_safety')
        ledger.require(ledger.canonical(safety) == ledger.canonical(SAFETY_CAPABILITY)
                       and format_for(prior) == COSINE_RECORD,
                       'project safety requires scoped records and the issued Evals profile')
        return SAFETY_RECORD
    if 'eval_embedding_grader' in capabilities:
        prior = dict(capabilities)
        grader = prior.pop('eval_embedding_grader')
        ledger.require(ledger.canonical(grader) == ledger.canonical(COSINE_GRADER)
                       and ledger.canonical(prior.get('evals')) == ledger.canonical(COSINE_EVALS)
                       and prior.get('vector_stores', {}).get('native_embedding_configured') is True,
                       'trained grading requires its exact enabled model and metric')
        prior['evals'] = EVAL_CAPABILITY
        ledger.require(format_for(prior) == EVAL_RECORD, 'missing qualified Evals predecessor')
        return COSINE_RECORD
    if 'evals' in capabilities:
        prior = dict(capabilities)
        selected = prior.pop('evals')
        ledger.require(ledger.canonical(selected) == ledger.canonical(EVAL_CAPABILITY)
                       and format_for(prior) == CHATKIT_RECORD,
                       'invalid Evals capabilities or predecessor')
        return EVAL_RECORD
    if 'chatkit' in capabilities:
        prior = dict(capabilities)
        selected = prior.pop('chatkit')
        ledger.require(ledger.canonical(selected) == ledger.canonical(CHATKIT_CAPABILITY)
                       and format_for(prior) == PREPARATION_RECORD,
                       'invalid ChatKit capability or predecessor')
        return CHATKIT_RECORD
    if 'hosted_environment_preparation' in capabilities:
        prior = dict(capabilities)
        selected = prior.pop('hosted_environment_preparation')
        ledger.require(ledger.canonical(selected) == ledger.canonical(PREPARATION_CAPABILITY)
                       and format_for(prior) == HOSTED_LIFECYCLE_RECORD,
                       'invalid hosted preparation declaration or predecessor')
        return PREPARATION_RECORD
    if 'hosted_environment_lifecycle' in capabilities:
        prior = dict(capabilities)
        selected = prior.pop('hosted_environment_lifecycle')
        ledger.require(ledger.canonical(selected) == ledger.canonical(HOSTED_LIFECYCLE_CAPABILITY)
                       and format_for(prior) == WEBHOOK_RECORD,
                       'invalid hosted workspace declaration or predecessor')
        return HOSTED_LIFECYCLE_RECORD
    if 'webhooks' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('webhooks')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(WEBHOOK_CAPABILITY)
                       and format_for(previous) == AUTOMATION_RECORD,
                       'invalid webhook declaration or predecessor')
        return WEBHOOK_RECORD
    if 'agent_automations' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('agent_automations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(AUTOMATION_CAPABILITY) and
                       ledger.canonical(previous.get('agent_sessions')) == ledger.canonical(automation_session()) and
                       format_for(dict(previous, agent_sessions=MANAGED_TOOL_SESSION)) == TRANSLATOR_RECORD,
                       'invalid automation or session declaration or predecessor')
        return AUTOMATION_RECORD
    if 'translations' in capabilities.get('realtime', {}):
        transport = dict(capabilities['realtime'])
        declaration = transport.pop('translations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(TRANSLATOR_CAPABILITY)
                       and format_for(dict(capabilities, realtime=transport)) == CODE_RECORD,
                       'translation transports or predecessor differ')
        return TRANSLATOR_RECORD
    if 'hosted_python' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('hosted_python')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(CODE_CAPABILITY)
                       and format_for(previous) == SKILLS_RECORD,
                       'interpreter declaration or workspace predecessor differs')
        return CODE_RECORD
    if 'container_workspaces' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('container_workspaces')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(SKILLS_CAPABILITY)
                       and format_for(previous) == ASSISTANTS_RECORD,
                       'workspace declaration or Assistants predecessor differs')
        return SKILLS_RECORD
    if 'assistants' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('assistants')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(ASSISTANTS_CAPABILITY)
                       and format_for(previous) == TRANSLATION_RECORD,
                       'Assistants qualification requires native Runs and the released predecessor')
        return ASSISTANTS_RECORD
    if 'translations' in capabilities:
        previous = dict(capabilities)
        declaration = previous.pop('translations')
        ledger.require(ledger.canonical(declaration) == ledger.canonical(TRANSLATION_CAPABILITY)
                       and format_for(previous) == SUBAGENT_RECORD,
                       'translation qualification requires both native models and its predecessor')
        return TRANSLATION_RECORD
    if 'agent_session_subagents' in capabilities:
        previous = dict(capabilities)
        child = previous.pop('agent_session_subagents')
        ledger.require(ledger.canonical(child) == ledger.canonical(SUBAGENT_CAPABILITY)
                       and format_for(previous) == MANAGED_TOOL_RECORD,
                       'incomplete managed subagent or predecessor declaration')
        return SUBAGENT_RECORD
    sessions = capabilities.get('agent_sessions')
    if isinstance(sessions, dict) and 'time_awareness' in sessions:
        ledger.require(ledger.canonical(sessions) == ledger.canonical(MANAGED_TOOL_SESSION) and
                       format_for(dict(capabilities, agent_sessions=WORKSPACE_SESSION)) == ARTIFACT_RECORD,
                       'incomplete managed tool or predecessor declaration')
        return MANAGED_TOOL_RECORD
    if 'agent_session_artifacts' in capabilities:
        previous = dict(capabilities)
        artifacts = previous.pop('agent_session_artifacts')
        ledger.require(ledger.canonical(artifacts) == ledger.canonical(ARTIFACT_CAPABILITY) and
                       format_for(previous) == WORKSPACE_RECORD,
                       'artifact qualification requires immutable files and hosted workspaces')
        return ARTIFACT_RECORD
    if 'agent_environments' in capabilities:
        previous = dict(capabilities)
        workspace = previous.pop('agent_environments')
        ledger.require(ledger.canonical(workspace) == ledger.canonical(WORKSPACE_CAPABILITY) and
                       ledger.canonical(previous.get('agent_sessions')) == ledger.canonical(WORKSPACE_SESSION),
                       'invalid hosted workspace capabilities')
        previous['agent_sessions'] = OAUTH_SESSION
        ledger.require(format_for(previous) == ENVIRONMENT_RECORD,
                       'workspace requires published templates and traces')
        return WORKSPACE_RECORD
    if 'beta_query_selector' in capabilities.get('responses', {}):
        ledger.require(capabilities['responses']['beta_query_selector'] == 'beta=true',
                       'unknown qualified Responses Beta selector')
        if 'multi_agent' in capabilities['responses']:
            ledger.require(capabilities['responses']['multi_agent'] == dict(
                selector='responses_multi_agent=v1', default_max_concurrent_subagents=3,
                shared_output_budget=True, agent_events=True, actions=['spawn_agent',
                'interrupt_agent', 'list_agents', 'send_message', 'followup_task', 'wait_agent']),
                'unknown qualified hosted Responses agent capabilities')
            if 'agent_registration' in capabilities:
                ledger.require(capabilities['agent_registration'] == REGISTRATION_CAPABILITY,
                               'unknown registered Agent configuration capability')
                if 'agent_sessions' in capabilities:
                    if 'agent_vaults' in capabilities:
                        if (ledger.canonical(capabilities['agent_vaults']) == ledger.canonical(VAULT_CAPABILITY)
                                and ledger.canonical(capabilities['agent_sessions']) == ledger.canonical(OAUTH_SESSION)):
                            if 'agent_environment_templates' in capabilities or 'agent_session_traces' in capabilities:
                                ledger.require(ledger.canonical(capabilities.get('agent_environment_templates')) ==
                                               ledger.canonical(TEMPLATE_CAPABILITY) and
                                               ledger.canonical(capabilities.get('agent_session_traces')) ==
                                               ledger.canonical(SESSION_TRACE_CAPABILITY),
                                               'incomplete environment or session trace qualification')
                                return ENVIRONMENT_RECORD
                            return OAUTH_RECORD
                        ledger.require(ledger.canonical(capabilities['agent_vaults']) ==
                                       ledger.canonical(VAULT_CAPABILITY) and
                                       ledger.canonical(capabilities['agent_sessions']) ==
                                       ledger.canonical(VAULT_SESSION),
                                       'unknown Agent Vault or MCP session capabilities')
                        return VAULT_RECORD
                    ledger.require(capabilities['agent_sessions'] == SESSION_CAPABILITY,
                                   'unknown qualified Agent session capabilities')
                    return SESSION_RECORD
                return REGISTRATION_RECORD
            return AGENT_RECORD_FORMAT
        return BETA_RECORD_FORMAT
    return RECORD_FORMAT


def attach(value, prior):
    """Store only the fields needed to recover the exact accepted old record."""
    old = prior['identity']
    new = value['identity']
    previous = {k: prior[k] for k in METADATA}
    previous['engine'] = {k: old[k] for k in ('binary_sha256', 'protocol_sha256')}
    previous['ctimes'] = {n: row['ctime_ns'] for n, row in old['model_assets'].items()
                          if row['ctime_ns'] != new['model_assets'][n]['ctime_ns']}
    before, after = prior['capabilities'], value['capabilities']
    previous['capabilities'] = {k: v for k, v in before.items()
                                if k not in after or ledger.canonical(v) != ledger.canonical(after[k])}
    previous['added'] = sorted(after.keys() - before.keys())
    agent = value['format'] == AGENT_RECORD_FORMAT
    refresh = agent and prior['format'] == AGENT_RECORD_FORMAT
    registration = value['format'] == REGISTRATION_RECORD
    session = value['format'] == SESSION_RECORD
    vault = value['format'] == VAULT_RECORD
    oauth = value['format'] == OAUTH_RECORD
    templates = value['format'] == ENVIRONMENT_RECORD
    workspace = value['format'] == WORKSPACE_RECORD
    revision = value['format'] in REUSE_FORMAT and value['format'] != RECORD_FORMAT
    if value['format'] in (PROGRAM_SDK_RECORD, COMPACT_MEDIA_RECORD, COMPACT_WINDOW_RECORD, MCP_ERROR_RECORD, CACHE_RETENTION_RECORD, LAYER_RECORD, INTERRUPTED_RETRY_RECORD, GRID_RECORD, FORK_RECORD, CLIENT_SHELL_RECORD, PATCH_RECORD, LOCAL_SHELL_RECORD, APPROVAL_IDS_RECORD, COMPACT_RECORD, PARTS_RECORD, NULL_HISTORY_RECORD, TOOL_MAP_RECORD, REGION_RECORD, VIDEO_CLIP_RECORD, RESPONSE_FAILURE_RECORD, EVAL_CANCEL_RECORD, FRAME_RECORD, VISUAL_ANSWER_RECORD, MEDIA_HANDOFF_RECORD, SESSION_CONTROL_RECORD, CHATKIT_WIDGET_RECORD, CHATKIT_IMAGE_RECORD, CHATKIT_WORKFLOW_RECORD, CHATKIT_GENERATION_RECORD, CHATKIT_BROWSER_RECORD, CHATKIT_ACTIONS_RECORD, CHATKIT_RETRY_RECORD, STORED_REFUSAL_RECORD, TYPED_HISTORY_RECORD, TRACK_RECORD, CHARACTER_RECORD, COMPOSER_RECORD, PAGINATION_RECORD, UPLOAD_RECORD, FEEDBACK_RECORD, METEOR_RECORD, HISTORY_RECORD, SAFETY_RECORD, COSINE_RECORD, EVAL_RECORD, CHATKIT_RECORD, PREPARATION_RECORD, HOSTED_LIFECYCLE_RECORD, WEBHOOK_RECORD, AUTOMATION_RECORD):
        predecessors = {
            PROGRAM_SDK_RECORD: (COMPACT_MEDIA_RECORD, COMPACT_MEDIA_REVIEWED, None, PROGRAM_SDK_RECEIPT),
            COMPACT_MEDIA_RECORD: (MCP_ERROR_RECORD, MCP_ERROR_REVIEWED, None, COMPACT_MEDIA_RECEIPT),
            COMPACT_WINDOW_RECORD: (MCP_ERROR_RECORD, MCP_ERROR_REVIEWED, None, COMPACT_WINDOW_RECEIPT),
            MCP_ERROR_RECORD: (CACHE_RETENTION_RECORD, CACHE_RETENTION_REVIEWED, None, MCP_ERROR_RECEIPT),
            CACHE_RETENTION_RECORD: (LAYER_RECORD, LAYER_REVIEWED, None, CACHE_RETENTION_RECEIPT),
            LAYER_RECORD: (INTERRUPTED_RETRY_RECORD, INTERRUPTED_RETRY_REVIEWED, None, LAYER_RECEIPT),
            INTERRUPTED_RETRY_RECORD: (GRID_RECORD, GRID_REVIEWED, None, INTERRUPTED_RETRY_RECEIPT),
            GRID_RECORD: (FORK_RECORD, FORK_REVIEWED, None, GRID_RECEIPT),
            FORK_RECORD: (CLIENT_SHELL_RECORD, CLIENT_SHELL_REVIEWED, None, FORK_RECEIPT),
            CLIENT_SHELL_RECORD: (PATCH_RECORD, PATCH_REVIEWED, None, CLIENT_SHELL_RECEIPT),
            PATCH_RECORD: (LOCAL_SHELL_RECORD, LOCAL_SHELL_REVIEWED, None, PATCH_RECEIPT),
            LOCAL_SHELL_RECORD: (APPROVAL_IDS_RECORD, APPROVAL_IDS_REVIEWED, None, LOCAL_SHELL_RECEIPT),
            APPROVAL_IDS_RECORD: (COMPACT_RECORD, COMPACT_REVIEWED, None, APPROVAL_IDS_RECEIPT),
            COMPACT_RECORD: (PARTS_RECORD, PARTS_REVIEWED, None, COMPACT_RECEIPT),
            PARTS_RECORD: (NULL_HISTORY_RECORD, NULL_HISTORY_REVIEWED, None, PARTS_RECEIPT),
            NULL_HISTORY_RECORD: (TOOL_MAP_RECORD, TOOL_MAP_REVIEWED, None, NULL_HISTORY_RECEIPT),
            TOOL_MAP_RECORD: (REGION_RECORD, REGION_REVIEWED, None, TOOL_MAP_RECEIPT),
            REGION_RECORD: (VIDEO_CLIP_RECORD, VIDEO_CLIP_REVIEWED, None, REGION_RECEIPT),
            VIDEO_CLIP_RECORD: (RESPONSE_FAILURE_RECORD, RESPONSE_FAILURE_REVIEWED, None, VIDEO_CLIP_RECEIPT),
            RESPONSE_FAILURE_RECORD: (EVAL_CANCEL_RECORD, EVAL_CANCEL_REVIEWED, None, RESPONSE_FAILURE_RECEIPT),
            EVAL_CANCEL_RECORD: (FRAME_RECORD, FRAME_REVIEWED, None, EVAL_CANCEL_RECEIPT),
            FRAME_RECORD: (VISUAL_ANSWER_RECORD, VISUAL_ANSWER_REVIEWED, None, FRAME_RECEIPT),
            VISUAL_ANSWER_RECORD: (MEDIA_HANDOFF_RECORD, MEDIA_HANDOFF_REVIEWED, None, VISUAL_ANSWER_RECEIPT),
            MEDIA_HANDOFF_RECORD: (SESSION_CONTROL_RECORD, SESSION_CONTROL_REVIEWED, None, MEDIA_HANDOFF_RECEIPT),
            SESSION_CONTROL_RECORD: (CHATKIT_WIDGET_RECORD, CHATKIT_WIDGET_REVIEWED, None, SESSION_CONTROL_RECEIPT),
            CHATKIT_WIDGET_RECORD: (CHATKIT_IMAGE_RECORD, CHATKIT_IMAGE_REVIEWED, None, CHATKIT_WIDGET_RECEIPT),
            CHATKIT_IMAGE_RECORD: (CHATKIT_WORKFLOW_RECORD, CHATKIT_WORKFLOW_REVIEWED, None, CHATKIT_IMAGE_RECEIPT),
            CHATKIT_WORKFLOW_RECORD: (CHATKIT_GENERATION_RECORD, CHATKIT_GENERATION_REVIEWED, None, CHATKIT_WORKFLOW_RECEIPT),
            CHATKIT_GENERATION_RECORD: (CHATKIT_BROWSER_RECORD, CHATKIT_BROWSER_REVIEWED, None, CHATKIT_GENERATION_RECEIPT),
            CHATKIT_BROWSER_RECORD: (CHATKIT_ACTIONS_RECORD, CHATKIT_ACTIONS_REVIEWED, None, CHATKIT_BROWSER_RECEIPT),
            CHATKIT_ACTIONS_RECORD: (CHATKIT_RETRY_RECORD, CHATKIT_RETRY_REVIEWED, None, CHATKIT_ACTIONS_RECEIPT),
            CHATKIT_RETRY_RECORD: (STORED_REFUSAL_RECORD, STORED_REFUSAL_REVIEWED, None, CHATKIT_RETRY_RECEIPT),
            STORED_REFUSAL_RECORD: (TYPED_HISTORY_RECORD, TYPED_HISTORY_REVIEWED, None, STORED_REFUSAL_RECEIPT),
            TYPED_HISTORY_RECORD: (TRACK_RECORD, TRACK_REVIEWED, None, TYPED_HISTORY_RECEIPT),
            TRACK_RECORD: (CHARACTER_RECORD, CHARACTER_REVIEWED, None, TRACK_RECEIPT),
            CHARACTER_RECORD: (COMPOSER_RECORD, COMPOSER_REVIEWED, 'video_characters', CHARACTER_RECEIPT),
            COMPOSER_RECORD: (PAGINATION_RECORD, PAGINATION_REVIEWED, None, COMPOSER_RECEIPT),
            PAGINATION_RECORD: (UPLOAD_RECORD, UPLOAD_REVIEWED, None, PAGINATION_RECEIPT),
            UPLOAD_RECORD: (FEEDBACK_RECORD, FEEDBACK_REVIEWED, None, 'chatkit-upload-handoff.json'),
            FEEDBACK_RECORD: (METEOR_RECORD, METEOR_REVIEWED, None, 'chatkit-feedback-handoff.json'),
            METEOR_RECORD: (HISTORY_RECORD, HISTORY_REVIEWED, None, 'eval-meteor-handoff.json'),
            HISTORY_RECORD: (SAFETY_RECORD, SAFETY_REVIEWED, None, 'history-arguments-handoff.json'),
            SAFETY_RECORD: (COSINE_RECORD, COSINE_REVIEWED, 'project_safety', 'project-safety-handoff.json'),
            COSINE_RECORD: (EVAL_RECORD, EVAL_REVIEWED, 'eval_embedding_grader', 'eval-cosine-handoff.json'),
            EVAL_RECORD: (CHATKIT_RECORD, CHATKIT_REVIEWED, 'evals', 'eval-handoff.json'),
            CHATKIT_RECORD: (PREPARATION_RECORD, PREPARATION_REVIEWED, 'chatkit', 'chatkit-handoff.json'),
            PREPARATION_RECORD: (HOSTED_LIFECYCLE_RECORD, HOSTED_LIFECYCLE_REVIEWED,
                                 'hosted_environment_preparation', 'preparation-handoff.json'),
            HOSTED_LIFECYCLE_RECORD: (WEBHOOK_RECORD, WEBHOOK_REVIEWED, 'hosted_environment_lifecycle',
                                      'environment-handoff.json'),
            WEBHOOK_RECORD: (AUTOMATION_RECORD, AUTOMATION_REVIEWED, 'webhooks', 'webhook-handoff.json'),
            AUTOMATION_RECORD: (TRANSLATOR_RECORD, TRANSLATOR_REVIEWED, 'agent_automations', 'automation-handoff.json'),
        }
        previous_format, reviewed, added, receipt = predecessors[value['format']]
        changed = {'protocol_source_sha256'}
        if value['format'] == CLIENT_SHELL_RECORD:
            changed.add('responses')
            ledger.require(ledger.canonical(after['responses']) ==
                           ledger.canonical(client_shell_capability(before['responses'])),
                           'only the qualified client shell contract may change')
        if value['format'] == PATCH_RECORD:
            changed.add('responses')
            ledger.require(ledger.canonical(after['responses']) ==
                           ledger.canonical(client_patch_capability(before['responses'])),
                           'only the qualified patch contract may change')
        if value['format'] == LOCAL_SHELL_RECORD:
            changed.add('responses')
            ledger.require(ledger.canonical(after['responses']) ==
                           ledger.canonical(local_shell_capability(before['responses'])),
                           'only the qualified client shell contract may change')
        if value['format'] in (VIDEO_CLIP_RECORD, REGION_RECORD, GRID_RECORD, LAYER_RECORD):
            upgrade = {VIDEO_CLIP_RECORD: clipped_file_inputs, REGION_RECORD: region_file_inputs,
                       GRID_RECORD: track_grid_file_inputs, LAYER_RECORD: layer_file_inputs}[value['format']]
            changed.add('file_inputs')
            ledger.require(ledger.canonical(after['file_inputs']) ==
                           ledger.canonical(upgrade(before['file_inputs'])),
                           'only the qualified video clipping declaration may change')
        if value['format'] in (TYPED_HISTORY_RECORD, VISUAL_ANSWER_RECORD, FRAME_RECORD, TOOL_MAP_RECORD, NULL_HISTORY_RECORD):
            changed.add('template')
            renderer = after['template'].get('renderer_source_sha256')
            ledger.require(isinstance(renderer, str) and ledger.HASH.fullmatch(renderer)
                           and renderer != before['template']['renderer_source_sha256']
                           and ledger.canonical(dict(before['template'], renderer_source_sha256=renderer))
                           == ledger.canonical(after['template']),
                           'only the qualified template renderer can change')
        if value['format'] == HISTORY_RECORD:
            changed.update(('template', 'tools'))
            template = dict(before['template'])
            for field in ('renderer_source_sha256', 'tool_history_source_sha256'):
                fingerprint = after['template'].get(field)
                ledger.require(type(fingerprint) is str and ledger.HASH.fullmatch(fingerprint),
                               'missing qualified tool history implementation')
                template[field] = fingerprint
            ledger.require(before['tools']['history'] == ISSUED_TOOL_HISTORY
                           and ledger.canonical(dict(before['tools'], history=REPAIRED_TOOL_HISTORY))
                           == ledger.canonical(after['tools'])
                           and ledger.canonical(template) == ledger.canonical(after['template']),
                           'unreviewed tool or template behavior changed')
        if value['format'] in (FEEDBACK_RECORD, UPLOAD_RECORD, COMPOSER_RECORD):
            changed.add('chatkit')
        if value['format'] in (COSINE_RECORD, METEOR_RECORD):
            changed.add('evals')
        if value['format'] == AUTOMATION_RECORD:
            changed.add('agent_sessions')
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == previous_format and
                       record_hash(prior) == reviewed.get(prior['profile']) and
                       previous['added'] == ([added] if added else []) and
                       set(previous['capabilities']) == changed and
                       format_for(after, proofs) == value['format'] and
                       old.keys() | {receipt} == proofs.keys() and
                       all(proofs[k] == v for k,v in old.items() if k not in
                           ('configuration.wire','capabilities.json','core-after.wire',
                            'observation.json','result.json','cleanup.json','plan.json')) and
                       ledger.HASH.fullmatch(proofs[receipt]),
                       'qualification needs the issued profile and pending delivery handoff')
    elif value['format'] == TRANSLATOR_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        expected = dict(before['realtime'], translations=TRANSLATOR_CAPABILITY)
        ledger.require(prior['format'] == CODE_RECORD and
                       record_hash(prior) == CODE_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256','realtime'} and
                       ledger.canonical(after['realtime']) == ledger.canonical(expected) and
                       format_for(after) == TRANSLATOR_RECORD and
                       old.keys() | {'translation-transport.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['translation-transport.json']),
                       'translation qualification needs the issued predecessor and both transports')
    elif value['format'] == CODE_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == SKILLS_RECORD and
                       record_hash(prior) == SKILLS_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['hosted_python'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == CODE_RECORD and old.keys() | {'code-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['code-handoff.json']),
                       'interpreter qualification requires the selected workspace record and SDK handoff')
    elif value['format'] == SKILLS_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ASSISTANTS_RECORD and
                       record_hash(prior) == ASSISTANTS_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['container_workspaces'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == SKILLS_RECORD and
                       old.keys() | {'skill-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['skill-handoff.json']),
                       'workspace qualification requires the released predecessor and skill handoff')
    elif value['format'] == ASSISTANTS_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == TRANSLATION_RECORD and
                       record_hash(prior) == TRANSLATION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['assistants'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == ASSISTANTS_RECORD and
                       old_proofs.keys() | {'assistant-handoff.json'} == proofs.keys() and
                       ledger.HASH.fullmatch(proofs['assistant-handoff.json']),
                       'Assistants qualification requires the reviewed predecessor and SDK handoff')
    elif value['format'] == TRANSLATION_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        additions = {'translations.json', 'beta-conversation.json', 'beta-transcription.json'}
        ledger.require(prior['format'] == SUBAGENT_RECORD and
                       record_hash(prior) in SUBAGENT_REVIEWED.get(prior['profile'], ()) and
                       previous['added'] == ['translations'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == TRANSLATION_RECORD and
                       old_proofs.keys() | additions == proofs.keys() and
                       all(ledger.HASH.fullmatch(proofs[k]) for k in additions),
                       'translation qualification requires the reviewed predecessor and three SDK receipts')
    elif value['format'] == SUBAGENT_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == MANAGED_TOOL_RECORD
                       and record_hash(prior) == MANAGED_TOOL_REVIEWED.get(prior['profile'])
                       and previous['added'] == ['agent_session_subagents']
                       and set(previous['capabilities']) == {'protocol_source_sha256'}
                       and format_for(after) == SUBAGENT_RECORD
                       and old_proofs.keys() | {'subagent-handoff.json'} == proofs.keys()
                       and ledger.HASH.fullmatch(proofs['subagent-handoff.json']),
                       'subagent qualification requires the reviewed tool predecessor and a new child handoff')
    elif value['format'] == MANAGED_TOOL_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        old_proofs = prior['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ARTIFACT_RECORD and
                       record_hash(prior) == ARTIFACT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == MANAGED_TOOL_RECORD and old_proofs.keys() <= proofs.keys() and
                       proofs['session-handoff.json'] != old_proofs['session-handoff.json'],
                       'managed tools require a qualified artifact predecessor and fresh tool handoff')
    elif value['format'] == ARTIFACT_RECORD:
        proofs = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == WORKSPACE_RECORD and
                       record_hash(prior) in WORKSPACE_REVIEWED.get(prior['profile'], ()) and
                       previous['added'] == ['agent_session_artifacts'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       format_for(after) == ARTIFACT_RECORD and
                       set(prior['business']['live']['evidence_sha256']) <= set(proofs) and
                       proofs['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'artifacts require the qualified workspace and a fresh file handoff')
    elif workspace:
        proofs = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == ENVIRONMENT_RECORD and
                       record_hash(prior) == ENVIRONMENT_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_environments'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == WORKSPACE_RECORD and
                       ledger.canonical(before['agent_sessions']) == ledger.canonical(OAUTH_SESSION) and
                       {'oauth-handoff.json', 'vault-handoff.json', 'registration.json',
                        'interruption.json', 'session-handoff.json'} <= proofs.keys() and
                       proofs['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'workspace requires the reviewed template predecessor and a new session handoff')
    elif templates:
        evidence = value['business']['live']['evidence_sha256']
        ledger.require(prior['format'] == OAUTH_RECORD and record_hash(prior) == OAUTH_REVIEWED.get(prior['profile'])
                       and previous['added'] == ['agent_environment_templates', 'agent_session_traces']
                       and set(previous['capabilities']) == {'protocol_source_sha256'}
                       and format_for(after) == ENVIRONMENT_RECORD
                       and {'oauth-handoff.json', 'vault-handoff.json', 'session-handoff.json',
                            'registration.json', 'interruption.json'} <= evidence.keys()
                       and evidence['session-handoff.json'] != prior['business']['live']['evidence_sha256']['session-handoff.json'],
                       'templates and traces need the reviewed OAuth predecessor and a fresh cross-capacity handoff')
    elif oauth:
        ledger.require(prior['format'] == VAULT_RECORD and
                       record_hash(prior) == VAULT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == OAUTH_RECORD and
                       {'oauth-handoff.json', 'vault-handoff.json', 'session-handoff.json',
                        'registration.json', 'interruption.json'} <= value['business']['live']['evidence_sha256'].keys(),
                       'OAuth qualification requires the accepted Vault predecessor and actual renewal')
    elif vault:
        ledger.require(prior['format'] == SESSION_RECORD and
                       record_hash(prior) == SESSION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_vaults'] and
                       set(previous['capabilities']) == {'protocol_source_sha256', 'agent_sessions'} and
                       format_for(after) == VAULT_RECORD and
                       {'vault-handoff.json', 'session-handoff.json', 'registration.json',
                        'interruption.json'} <= value['business']['live']['evidence_sha256'].keys(),
                       'Vault qualification needs the accepted session and real credential handoff')
    elif registration:
        from tools.serve_recovery import profile_duplex_reuse
        ledger.require(prior['format'] == AGENT_RECORD_FORMAT and
                       record_hash(prior) == REFRESH_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_registration'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       value['capabilities']['agent_registration'] == REGISTRATION_CAPABILITY and
                       {'interruption.json','registration.json'} <=
                       value['business']['live']['evidence_sha256'].keys(),
                       'registration must extend the qualified recovery and SDK business')
        value['business']['realtime_duplex'] = profile_duplex_reuse.pack_text(
            value['business']['realtime_duplex'])
    elif session:
        ledger.require(prior['format'] == REGISTRATION_RECORD and
                       record_hash(prior) == REGISTRATION_REVIEWED.get(prior['profile']) and
                       previous['added'] == ['agent_sessions'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'} and
                       after['agent_sessions'] == SESSION_CAPABILITY and
                       {'session-handoff.json','registration.json','interruption.json'} <=
                       value['business']['live']['evidence_sha256'].keys(),
                       'session qualification needs retained registration, recovery and a real handoff')
    elif refresh:
        ledger.require(record_hash(prior) == AGENT_REVIEWED.get(prior['profile']) and
                       not previous['added'] and
                       set(previous['capabilities']) == {'protocol_source_sha256'},
                       'requalification must preserve the reviewed agent capabilities')
    elif revision:
        ledger.require(prior['format'] == (BETA_RECORD_FORMAT if agent else RECORD_FORMAT),
                       'new Responses qualification requires its accepted predecessor')
    if revision:
        previous['live'] = prior['business']['live']
    if agent and not refresh:
        original = dict(after['responses'])
        original.pop('multi_agent')
        ledger.require(original == before['responses'], 'unreviewed Responses capability difference')
        del previous['capabilities']['responses']
    envelope = dict(format=AGENT_REFRESH if refresh else REUSE_FORMAT[value['format']])
    if value['format'] in SEPARATE_HISTORY_RECORDS:
        # Prior releases already compressed their history. Keep those signed
        # bytes once and compress only this release's reconstruction fields.
        envelope['history'] = previous.pop('reused_business')
    envelope['previous'] = codec.pack(previous)
    value['reused_business'] = envelope
    ledger.require(record_hash(previous_record(value)) == record_hash(prior),
                   'Live qualification cannot reconstruct its accepted source')


def previous_record(value):
    reuse = value['reused_business']
    separate_history = value['format'] in SEPARATE_HISTORY_RECORDS
    ledger.keys(reuse, ('format', 'previous', 'history') if separate_history else ('format', 'previous'))
    agent = value['format'] == AGENT_RECORD_FORMAT
    refresh = agent and reuse['format'] == AGENT_REFRESH
    registration = value['format'] == REGISTRATION_RECORD
    session = value['format'] == SESSION_RECORD
    revision = value['format'] in REUSE_FORMAT and value['format'] != RECORD_FORMAT
    ledger.require(refresh or reuse['format'] == REUSE_FORMAT.get(value['format']),
                   'incorrect Live qualification source')
    previous = codec.unpack(reuse['previous'])
    if separate_history:
        history_format, history_keys = {
            PROGRAM_SDK_RECORD: (COMPACT_MEDIA_FORMAT, ('format', 'previous', 'history')),
            COMPACT_MEDIA_RECORD: (MCP_ERROR_FORMAT, ('format', 'previous', 'history')),
            COMPACT_WINDOW_RECORD: (MCP_ERROR_FORMAT, ('format', 'previous', 'history')),
            MCP_ERROR_RECORD: (CACHE_RETENTION_FORMAT, ('format', 'previous', 'history')),
            CACHE_RETENTION_RECORD: (LAYER_FORMAT, ('format', 'previous', 'history')),
            LAYER_RECORD: (INTERRUPTED_RETRY_FORMAT, ('format', 'previous', 'history')),
            INTERRUPTED_RETRY_RECORD: (GRID_FORMAT, ('format', 'previous', 'history')),
            GRID_RECORD: (FORK_FORMAT, ('format', 'previous', 'history')),
            FORK_RECORD: (CLIENT_SHELL_FORMAT, ('format', 'previous', 'history')),
            CLIENT_SHELL_RECORD: (PATCH_FORMAT, ('format', 'previous', 'history')),
            PATCH_RECORD: (LOCAL_SHELL_FORMAT, ('format', 'previous', 'history')),
            LOCAL_SHELL_RECORD: (APPROVAL_IDS_FORMAT, ('format', 'previous', 'history')),
            APPROVAL_IDS_RECORD: (COMPACT_FORMAT, ('format', 'previous', 'history')),
            COMPACT_RECORD: (PARTS_FORMAT, ('format', 'previous', 'history')),
            PARTS_RECORD: (NULL_HISTORY_FORMAT, ('format', 'previous', 'history')),
            NULL_HISTORY_RECORD: (TOOL_MAP_FORMAT, ('format', 'previous', 'history')),
            TOOL_MAP_RECORD: (REGION_FORMAT, ('format', 'previous', 'history')),
            REGION_RECORD: (VIDEO_CLIP_FORMAT, ('format', 'previous', 'history')),
            VIDEO_CLIP_RECORD: (RESPONSE_FAILURE_FORMAT, ('format', 'previous', 'history')),
            RESPONSE_FAILURE_RECORD: (EVAL_CANCEL_FORMAT, ('format', 'previous', 'history')),
            EVAL_CANCEL_RECORD: (FRAME_FORMAT, ('format', 'previous', 'history')),
            FRAME_RECORD: (VISUAL_ANSWER_FORMAT, ('format', 'previous', 'history')),
            VISUAL_ANSWER_RECORD: (MEDIA_HANDOFF_FORMAT, ('format', 'previous', 'history')),
            MEDIA_HANDOFF_RECORD: (SESSION_CONTROL_FORMAT, ('format', 'previous', 'history')),
            SESSION_CONTROL_RECORD: (CHATKIT_WIDGET_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_WIDGET_RECORD: (CHATKIT_IMAGE_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_IMAGE_RECORD: (CHATKIT_WORKFLOW_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_WORKFLOW_RECORD: (CHATKIT_GENERATION_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_GENERATION_RECORD: (CHATKIT_BROWSER_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_BROWSER_RECORD: (CHATKIT_ACTIONS_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_ACTIONS_RECORD: (CHATKIT_RETRY_FORMAT, ('format', 'previous', 'history')),
            CHATKIT_RETRY_RECORD: (STORED_REFUSAL_FORMAT, ('format', 'previous', 'history')),
            STORED_REFUSAL_RECORD: (TYPED_HISTORY_FORMAT, ('format', 'previous', 'history')),
            TYPED_HISTORY_RECORD: (TRACK_FORMAT, ('format', 'previous', 'history')),
            TRACK_RECORD: (CHARACTER_FORMAT, ('format', 'previous', 'history')),
            CHARACTER_RECORD: (COMPOSER_FORMAT, ('format', 'previous', 'history')),
            COMPOSER_RECORD: (PAGINATION_FORMAT, ('format', 'previous', 'history')),
            PAGINATION_RECORD: (UPLOAD_FORMAT, ('format', 'previous', 'history')),
            UPLOAD_RECORD: (FEEDBACK_FORMAT, ('format', 'previous', 'history')),
            FEEDBACK_RECORD: (METEOR_FORMAT, ('format', 'previous', 'history')),
            METEOR_RECORD: (HISTORY_FORMAT, ('format', 'previous', 'history')),
            HISTORY_RECORD: (SAFETY_FORMAT, ('format', 'previous', 'history')),
            SAFETY_RECORD: (COSINE_FORMAT, ('format', 'previous', 'history')),
            COSINE_RECORD: (EVAL_FORMAT, ('format', 'previous')),
        }[value['format']]
        ledger.require('reused_business' not in previous and
                       type(reuse['history']) is dict and
                       reuse['history'].get('format') == history_format,
                       'unexpected issued predecessor history')
        ledger.keys(reuse['history'], history_keys)
        previous['reused_business'] = reuse['history']
    fields = (*METADATA, 'engine', 'ctimes', 'capabilities', 'added')
    ledger.keys(previous, (*fields, 'live') if revision else fields)
    original = copy.deepcopy(value)
    original.update({k: previous[k] for k in METADATA})
    original['identity'].update(previous['engine'])
    for name, ctime in previous['ctimes'].items():
        original['identity']['model_assets'][name]['ctime_ns'] = ctime
    for name in previous['added']:
        del original['capabilities'][name]
    original['capabilities'].update(previous['capabilities'])
    if agent and not refresh:
        del original['capabilities']['responses']['multi_agent']
    if registration:
        from tools.serve_recovery import profile_duplex_reuse
        original['business']['realtime_duplex'] = profile_duplex_reuse.unpack_text(
            original['business']['realtime_duplex'])
    if revision:
        original['business']['live'] = previous['live']
    else:
        del original['business']['live']
    return original


def validate(value):
    from tools.serve_recovery import profile_trace_reuse
    if value['format'] in (PROGRAM_SDK_RECORD, COMPACT_MEDIA_RECORD, COMPACT_WINDOW_RECORD, MCP_ERROR_RECORD, CACHE_RETENTION_RECORD, LAYER_RECORD, INTERRUPTED_RETRY_RECORD, GRID_RECORD, FORK_RECORD, CLIENT_SHELL_RECORD, PATCH_RECORD, LOCAL_SHELL_RECORD, APPROVAL_IDS_RECORD, COMPACT_RECORD, PARTS_RECORD, NULL_HISTORY_RECORD, TOOL_MAP_RECORD, REGION_RECORD, VIDEO_CLIP_RECORD, RESPONSE_FAILURE_RECORD, EVAL_CANCEL_RECORD, FRAME_RECORD, VISUAL_ANSWER_RECORD, MEDIA_HANDOFF_RECORD, SESSION_CONTROL_RECORD, CHATKIT_WIDGET_RECORD, CHATKIT_IMAGE_RECORD, CHATKIT_WORKFLOW_RECORD, CHATKIT_GENERATION_RECORD, CHATKIT_BROWSER_RECORD, CHATKIT_ACTIONS_RECORD, CHATKIT_RETRY_RECORD, STORED_REFUSAL_RECORD, TYPED_HISTORY_RECORD, TRACK_RECORD, CHARACTER_RECORD, COMPOSER_RECORD, PAGINATION_RECORD, UPLOAD_RECORD, FEEDBACK_RECORD, METEOR_RECORD, HISTORY_RECORD, SAFETY_RECORD, COSINE_RECORD, EVAL_RECORD, CHATKIT_RECORD, PREPARATION_RECORD, HOSTED_LIFECYCLE_RECORD, WEBHOOK_RECORD, AUTOMATION_RECORD):
        current, previous = {
            PROGRAM_SDK_RECORD: (PROGRAM_SDK_REVIEWED, COMPACT_MEDIA_REVIEWED),
            COMPACT_MEDIA_RECORD: (COMPACT_MEDIA_REVIEWED, MCP_ERROR_REVIEWED),
            COMPACT_WINDOW_RECORD: (COMPACT_WINDOW_REVIEWED, MCP_ERROR_REVIEWED),
            MCP_ERROR_RECORD: (MCP_ERROR_REVIEWED, CACHE_RETENTION_REVIEWED),
            CACHE_RETENTION_RECORD: (CACHE_RETENTION_REVIEWED, LAYER_REVIEWED),
            LAYER_RECORD: (LAYER_REVIEWED, INTERRUPTED_RETRY_REVIEWED),
            INTERRUPTED_RETRY_RECORD: (INTERRUPTED_RETRY_REVIEWED, GRID_REVIEWED),
            GRID_RECORD: (GRID_REVIEWED, FORK_REVIEWED),
            FORK_RECORD: (FORK_REVIEWED, CLIENT_SHELL_REVIEWED),
            CLIENT_SHELL_RECORD: (CLIENT_SHELL_REVIEWED, PATCH_REVIEWED),
            PATCH_RECORD: (PATCH_REVIEWED, LOCAL_SHELL_REVIEWED),
            LOCAL_SHELL_RECORD: (LOCAL_SHELL_REVIEWED, APPROVAL_IDS_REVIEWED),
            APPROVAL_IDS_RECORD: (APPROVAL_IDS_REVIEWED, COMPACT_REVIEWED),
            COMPACT_RECORD: (COMPACT_REVIEWED, PARTS_REVIEWED),
            PARTS_RECORD: (PARTS_REVIEWED, NULL_HISTORY_REVIEWED),
            NULL_HISTORY_RECORD: (NULL_HISTORY_REVIEWED, TOOL_MAP_REVIEWED),
            TOOL_MAP_RECORD: (TOOL_MAP_REVIEWED, REGION_REVIEWED),
            REGION_RECORD: (REGION_REVIEWED, VIDEO_CLIP_REVIEWED),
            VIDEO_CLIP_RECORD: (VIDEO_CLIP_REVIEWED, RESPONSE_FAILURE_REVIEWED),
            RESPONSE_FAILURE_RECORD: (RESPONSE_FAILURE_REVIEWED, EVAL_CANCEL_REVIEWED),
            EVAL_CANCEL_RECORD: (EVAL_CANCEL_REVIEWED, FRAME_REVIEWED),
            FRAME_RECORD: (FRAME_REVIEWED, VISUAL_ANSWER_REVIEWED),
            VISUAL_ANSWER_RECORD: (VISUAL_ANSWER_REVIEWED, MEDIA_HANDOFF_REVIEWED),
            MEDIA_HANDOFF_RECORD: (MEDIA_HANDOFF_REVIEWED, SESSION_CONTROL_REVIEWED),
            SESSION_CONTROL_RECORD: (SESSION_CONTROL_REVIEWED, CHATKIT_WIDGET_REVIEWED),
            CHATKIT_WIDGET_RECORD: (CHATKIT_WIDGET_REVIEWED, CHATKIT_IMAGE_REVIEWED),
            CHATKIT_IMAGE_RECORD: (CHATKIT_IMAGE_REVIEWED, CHATKIT_WORKFLOW_REVIEWED),
            CHATKIT_WORKFLOW_RECORD: (CHATKIT_WORKFLOW_REVIEWED, CHATKIT_GENERATION_REVIEWED),
            CHATKIT_GENERATION_RECORD: (CHATKIT_GENERATION_REVIEWED, CHATKIT_BROWSER_REVIEWED),
            CHATKIT_BROWSER_RECORD: (CHATKIT_BROWSER_REVIEWED, CHATKIT_ACTIONS_REVIEWED),
            CHATKIT_ACTIONS_RECORD: (CHATKIT_ACTIONS_REVIEWED, CHATKIT_RETRY_REVIEWED),
            CHATKIT_RETRY_RECORD: (CHATKIT_RETRY_REVIEWED, STORED_REFUSAL_REVIEWED),
            STORED_REFUSAL_RECORD: (STORED_REFUSAL_REVIEWED, TYPED_HISTORY_REVIEWED),
            TYPED_HISTORY_RECORD: (TYPED_HISTORY_REVIEWED, TRACK_REVIEWED),
            TRACK_RECORD: (TRACK_REVIEWED, CHARACTER_REVIEWED),
            CHARACTER_RECORD: (CHARACTER_REVIEWED, COMPOSER_REVIEWED),
            COMPOSER_RECORD: (COMPOSER_REVIEWED, PAGINATION_REVIEWED),
            PAGINATION_RECORD: (PAGINATION_REVIEWED, UPLOAD_REVIEWED),
            UPLOAD_RECORD: (UPLOAD_REVIEWED, FEEDBACK_REVIEWED),
            FEEDBACK_RECORD: (FEEDBACK_REVIEWED, METEOR_REVIEWED),
            METEOR_RECORD: (METEOR_REVIEWED, HISTORY_REVIEWED),
            HISTORY_RECORD: (HISTORY_REVIEWED, SAFETY_REVIEWED),
            SAFETY_RECORD: (SAFETY_REVIEWED, COSINE_REVIEWED),
            COSINE_RECORD: (COSINE_REVIEWED, EVAL_REVIEWED),
            EVAL_RECORD: (EVAL_REVIEWED, CHATKIT_REVIEWED),
            CHATKIT_RECORD: (CHATKIT_REVIEWED, PREPARATION_REVIEWED),
            PREPARATION_RECORD: (PREPARATION_REVIEWED, HOSTED_LIFECYCLE_REVIEWED),
            HOSTED_LIFECYCLE_RECORD: (HOSTED_LIFECYCLE_REVIEWED, WEBHOOK_REVIEWED),
            WEBHOOK_RECORD: (WEBHOOK_REVIEWED, AUTOMATION_REVIEWED),
            AUTOMATION_RECORD: (AUTOMATION_REVIEWED, TRANSLATOR_REVIEWED),
        }[value['format']]
        digest = record_hash(value)
        allowed = {current.get(value['profile'])}
        if value['format'] == SESSION_CONTROL_RECORD:
            allowed.update(pins.get(value['profile']) for pins in
                           (SESSION_CONTROL_PILOT_REVIEWED, SESSION_CONTROL_RECOVERY_REVIEWED))
        ledger.require(digest in allowed, 'profile differs from the qualified delivery handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == previous.get(value['profile']),
                       'delivery handoff lost its issued predecessor')
        # Reconstruct the allowed migration from the retained predecessor.
        # The profile registry already binds this complete record by digest.
        reproduced = copy.deepcopy(value)
        attach(reproduced, prior)
        ledger.require(ledger.canonical(reproduced['reused_business']) ==
                       ledger.canonical(value['reused_business']),
                       'delivery handoff predecessor differs')
        validate(prior)
        return value['reused_business']
    if value['format'] == TRANSLATOR_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == TRANSLATOR_REVIEWED.get(name),
                       'translation record differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == CODE_REVIEWED.get(name),
                       'translation transport lost its issued predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == CODE_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == CODE_REVIEWED.get(name),
                       'interpreter record differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == SKILLS_REVIEWED.get(name),
                       'interpreter qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SKILLS_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == SKILLS_REVIEWED.get(name),
                       'skill workspace profile differs from its qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ASSISTANTS_REVIEWED.get(name),
                       'skill workspace qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ASSISTANTS_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ASSISTANTS_REVIEWED.get(name),
                       'Assistants qualification differs from the reviewed capacity')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == TRANSLATION_REVIEWED.get(name),
                       'Assistants qualification lost its released predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == TRANSLATION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == TRANSLATION_REVIEWED.get(name),
                       'translation qualification differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(record_hash(prior) in SUBAGENT_REVIEWED.get(name, ()),
                       'translation qualification lost the reviewed predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SUBAGENT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) in SUBAGENT_REVIEWED.get(name, ()),
                       'subagent qualification differs from the reviewed capacity handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == MANAGED_TOOL_REVIEWED.get(name),
                       'subagent qualification lost the managed tool predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == MANAGED_TOOL_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == MANAGED_TOOL_REVIEWED.get(name),
                       'managed tools differ from the reviewed capacity handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ARTIFACT_REVIEWED.get(name),
                       'managed tool qualification lost the artifact predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ARTIFACT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ARTIFACT_REVIEWED.get(name),
                       'artifact qualification differs from reviewed primary/light evidence')
        prior = previous_record(value)
        ledger.require(record_hash(prior) in WORKSPACE_REVIEWED.get(name, ()),
                       'artifact qualification lost the hosted workspace predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == WORKSPACE_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) in WORKSPACE_REVIEWED.get(name, ()),
                       'workspace differs from the reviewed handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == ENVIRONMENT_REVIEWED.get(name),
                       'workspace lost the reviewed template predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == ENVIRONMENT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == ENVIRONMENT_REVIEWED.get(name),
                       'template and session trace qualification differs from the reviewed handoff')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == OAUTH_REVIEWED.get(name),
                       'template qualification lost the OAuth predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == OAUTH_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == OAUTH_REVIEWED.get(name),
                       'OAuth qualification differs from the reviewed credential exchange')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == VAULT_REVIEWED.get(name),
                       'OAuth qualification lost the Vault predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == VAULT_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == VAULT_REVIEWED.get(name),
                       'Vault handoff differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == SESSION_REVIEWED.get(name),
                       'Vault qualification lost the session predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == SESSION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == SESSION_REVIEWED.get(name),
                       'session handoff differs from the reviewed qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == REGISTRATION_REVIEWED.get(name),
                       'session handoff lost the registered predecessor')
        validate(prior)
        return value['reused_business']
    if value['format'] == REGISTRATION_RECORD:
        name = value['profile']
        ledger.require(record_hash(value) == REGISTRATION_REVIEWED.get(name),
                       'Agent registration differs from its reviewed qualification')
        prior = previous_record(value)
        ledger.require(record_hash(prior) == REFRESH_REVIEWED.get(name),
                       'Agent registration lost the qualified interruption recovery')
        validate(prior)
        return value['reused_business']
    if value['format'] == AGENT_RECORD_FORMAT:
        name = value['profile']
        if value['reused_business'].get('format') == AGENT_REFRESH:
            ledger.require(record_hash(value) == REFRESH_REVIEWED.get(name),
                           'agent recovery differs from the reviewed profile')
            prior = previous_record(value)
            ledger.require(record_hash(prior) == AGENT_REVIEWED.get(name),
                           'agent recovery lost the registered hosted business')
            validate(prior)
            return value['reused_business']
        ledger.require(record_hash(value) == AGENT_REVIEWED.get(name),
                       'hosted agent qualification differs from the reviewed run')
        prior = previous_record(value)
        ledger.require(prior['format'] == BETA_RECORD_FORMAT and
                       record_hash(prior) == BETA_REVIEWED[name],
                       'hosted agent qualification lost the registered Beta source')
        validate(prior)
        return value['reused_business']
    if value['format'] == BETA_RECORD_FORMAT:
        name = value['profile']
        ledger.require(record_hash(value) == BETA_REVIEWED.get(name),
                       'Beta qualification differs from reviewed business and run identities')
        prior = previous_record(value)
        ledger.require(prior['format'] == RECORD_FORMAT and
                       record_hash(prior) == REVIEWED[name]['selected'],
                       'Beta qualification lost its accepted Live source')
        validate(prior)
        return value['reused_business']
    expected = REVIEWED.get(value['profile'])
    ledger.require(expected and value['format'] == RECORD_FORMAT and
                   record_hash(value) == expected['selected'],
                   'Live qualification differs from reviewed business and run identities')
    previous = previous_record(value)
    ledger.require(record_hash(previous) == expected['previous'],
                   'Live qualification lost historical business or its original failures')
    profile_trace_reuse.validate(previous)
    return value['reused_business']
