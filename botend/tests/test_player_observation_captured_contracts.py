"""Captured rollout AI contracts: untouched model output, no transport/DB dependency."""
import json
from django.test import SimpleTestCase
from botend.services import player_observation as observation


class CapturedInvestigationContractTests(SimpleTestCase):
    def test_old_prompt_cannot_reuse_even_matching_evidence_hash(self):
        from types import SimpleNamespace
        evidence = {'evidence_hash': 'same-facts'}
        previous = SimpleNamespace(prompt_version=6, evidence_hash='same-facts')
        self.assertFalse(observation._same_publication(previous, evidence))
        previous.prompt_version = observation.PROMPT_VERSION
        # Exact hash equality cannot bypass missing frozen evidence integrity.
        self.assertFalse(observation._same_publication(previous, evidence))

    def test_captured_raw_contracts(self):
        for case in CASES:
            with self.subTest(reject=case['reject']):
                if case['reject']:
                    with self.assertRaisesRegex(observation.InvalidResult, 'unsupported_inference_assertion'):
                        observation._validate(case['raw'], case['evidence'], require_investigation=True)
                    # Frozen parse remains readable, without rewriting the raw.
                    observation._validate(case['raw'], case['evidence'])
                else:
                    observation._validate(case['raw'], case['evidence'], require_investigation=True)


CASES = json.loads(r'''[
  {
    "raw": "{\"observations\":[{\"code\":\"joint_progress\",\"confidence\":\"低\",\"reason_id\":\"repeated_witnesses\",\"limitation_ids\":[\"sample_scope\",\"unknown_motive\"],\"evidence_ids\":[\"behavior:joint_progress:21163276\",\"behavior:joint_progress:21176694\",\"behavior:joint_progress:21243722\"]},{\"code\":\"recent_inquiry\",\"confidence\":\"低\",\"reason_id\":\"recent_clue\",\"limitation_ids\":[\"current_not_history\",\"unknown_motive\"],\"evidence_ids\":[\"behavior:interpretation_limits\"]}],\"conclusion_id\":\"public_sample\",\"investigation\":{\"summary\":\"多场同本通关呈现本人与固定同伴共同记录提高，其中一场开打前本人已有同本更高限时，升钥或库存解释保留。该线索仅为低置信事实候选，不能区分朋友互助、协助上分或钥匙协调；钥匙归属、收费与完整坚韧历史未知。需优先核验相关成员同本历史与开打前坚韧条件。\",\"confidence\":\"低\",\"hypotheses\":[{\"id\":\"joint_progress\",\"assessment\":\"supported\",\"evidence_ids\":[\"behavior:joint_progress:21163276\",\"behavior:joint_progress:21176694\",\"behavior:interpretation_limits\"],\"counter_evidence_ids\":[],\"explanation\":\"至少两场独立通关显示本人与至少三名同伴同本共同前场升层，成绩时序与成员关系支持共同提高这一低置信解释。反证未见，但钥匙归属、动机与收费未知，朋友互助也可同时属于协助，故仅限事实层面成立。\"},{\"id\":\"key_access\",\"assessment\":\"possible\",\"evidence_ids\":[\"behavior:interpretation_limits\"],\"counter_evidence_ids\":[],\"explanation\":\"本人此前同本更高，升钥或库存钥匙解释未被排除，全层跑前保护未完整证实，也不能确认本场钥匙归属或排除旧钥匙、他人钥匙。因此钥匙协调线索只能保持可能，未知不作为支持或反证。\"},{\"id\":\"assistance\",\"assessment\":\"possible\",\"evidence_ids\":[\"behavior:key_recovery_unknown:21243722\",\"behavior:interpretation_limits\"],\"counter_evidence_ids\":[],\"explanation\":\"固定五人阵容与共同提高可同时由朋友互助或协助上分解释，两者不互斥。本人已有同本更高观测削弱了对本人单纯被带高的解读，但对同伴是否受益于协助无从确认，需核验相关成员同本历史后比较。\"},{\"id\":\"routine_group\",\"assessment\":\"possible\",\"evidence_ids\":[\"behavior:interpretation_limits\"],\"counter_evidence_ids\":[],\"explanation\":\"近期阵容出现稳定组合，符合日常协作与朋友互助的模式；固定阵容本身不证明带人，也不排除协助上分。当前快照不能回填历史加分，后续低分不作硬否定，故仅能列为可能解释之一。\"}],\"next_question_ids\":[\"scored_history:3896650617c7088ce35291fc43158cf2e2ce41854d570a69011806109363569b:16359\",\"scored_history:3896650617c7088ce35291fc43158cf2e2ce41854d570a69011806109363569b:16865\",\"scored_runs:3e48e39a71fe7e749e35a5aa3cfefbc3fa6c5f186a3c471511337efa02a59a17\"]}}",
    "evidence": {
      "eligible_labels": [
        {
          "code": "joint_progress",
          "label": "共同提高成绩线索",
          "confidence_max": "低",
          "evidence_ids": [
            "behavior:joint_progress:21163276",
            "behavior:joint_progress:21176694",
            "behavior:joint_progress:21243722"
          ],
          "reason_options": {
            "repeated_witnesses": "至少两场独立通关的成绩时序与成员关系支持这一低置信解释。"
          },
          "limitation_options": {
            "sample_scope": "只代表已观测样本，不证明长期身份或完整历史。",
            "unknown_motive": "朋友互助可属于协助上分；钥匙归属及收费未知。"
          },
          "required_limitation_ids": [
            "sample_scope",
            "unknown_motive"
          ]
        },
        {
          "code": "recent_inquiry",
          "label": "近期协作线索（待解释）",
          "confidence_max": "低",
          "evidence_ids": [
            "behavior:interpretation_limits"
          ],
          "reason_options": {
            "recent_clue": "已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。"
          },
          "limitation_options": {
            "current_not_history": "当前成绩仅为代理，不代表历史当场加分。",
            "unknown_motive": "协作、练习和后续进步仍可能；无法确认钥匙归属或交易。"
          },
          "required_limitation_ids": [
            "current_not_history",
            "unknown_motive"
          ]
        }
      ],
      "evidence": [
        {
          "id": "timed_experience"
        },
        {
          "id": "behavior:lead:21135624"
        },
        {
          "id": "behavior:lead:21150049"
        },
        {
          "id": "behavior:joint_progress:21163276"
        },
        {
          "id": "behavior:joint_progress:21176694"
        },
        {
          "id": "behavior:prior_higher:21243722"
        },
        {
          "id": "behavior:key_recovery_unknown:21243722"
        },
        {
          "id": "behavior:joint_progress:21243722"
        },
        {
          "id": "behavior:interpretation_limits"
        }
      ],
      "metrics": {
        "investigation": {
          "triggered": true,
          "hypotheses": [
            {
              "id": "key_access",
              "label": "钥匙协调线索",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits"
              ]
            },
            {
              "id": "joint_progress",
              "label": "共同提高成绩线索",
              "status": "supported",
              "support_ids": [
                "behavior:joint_progress:21163276",
                "behavior:joint_progress:21176694",
                "behavior:joint_progress:21243722"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits"
              ]
            },
            {
              "id": "assistance",
              "label": "疑似协助他人上分",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "behavior:key_recovery_unknown:21243722"
              ]
            },
            {
              "id": "routine_group",
              "label": "日常协作与朋友互助",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits"
              ]
            }
          ],
          "questions": [
            {
              "id": "scored_history:3896650617c7088ce35291fc43158cf2e2ce41854d570a69011806109363569b:16359"
            },
            {
              "id": "scored_history:3896650617c7088ce35291fc43158cf2e2ce41854d570a69011806109363569b:16865"
            },
            {
              "id": "scored_history:3896650617c7088ce35291fc43158cf2e2ce41854d570a69011806109363569b:9527"
            },
            {
              "id": "scored_runs:3e48e39a71fe7e749e35a5aa3cfefbc3fa6c5f186a3c471511337efa02a59a17"
            }
          ]
        },
        "behavior_review": {
          "witnesses": {
            "carrying": [],
            "being_carried": [],
            "joint_progress": [
              21163276,
              21176694,
              21243722
            ],
            "key_access": [],
            "high_score_collaborator": []
          },
          "supported": {
            "joint_progress": [
              "behavior:joint_progress:21163276",
              "behavior:joint_progress:21176694",
              "behavior:joint_progress:21243722"
            ]
          }
        }
      }
    },
    "reject": false
  },
  {
    "raw": "{\n  \"observations\": [\n    {\n      \"code\": \"recent_inquiry\",\n      \"confidence\": \"低\",\n      \"reason_id\": \"recent_clue\",\n      \"limitation_ids\": [\"current_not_history\", \"unknown_motive\"],\n      \"evidence_ids\": [\"investigation:own:20827902\", \"investigation:team:20827902\", \"investigation:own:20828794\"]\n    }\n  ],\n  \"conclusion_id\": \"public_sample\",\n  \"investigation\": {\n    \"summary\": \"本人多次与同一四人稳定组合在同本以低于自身当前上限的层数限时，外围席位有轮换。该时序构成调查线索：可能为朋友互助、协助上分或日常练习。但本人曾在其中一场之后完成更高层，说明后续进步是真实且已获支持的解释方向；同时行为标签显示有同伴共同前场升层。钥匙归属、动机、收费及外围成员历史均未知，坚韧逐场开打前条件亦未完整证实，因此只能保持低置信并继续定向取证。\",\n    \"confidence\": \"低\",\n    \"hypotheses\": [\n      {\n        \"id\": \"key_access\",\n        \"assessment\": \"unknown\",\n        \"evidence_ids\": [\"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"钥匙协调无法由公开通关与当前快照判断：不知本场钥匙归属，也不排除旧钥匙或他人的钥匙。反复低层活动与钥匙解释兼容但同样与日常协作兼容，当前既无支持也无反证，只能列为未知，需逐场开打前坚韧证据再议。\"\n      },\n      {\n        \"id\": \"assistance\",\n        \"assessment\": \"unknown\",\n        \"evidence_ids\": [\"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"同一稳定组合反复带外围席位打同本较低层，与协助上分的形式相似，且有同伴共同前场升层的低置信候选。但外围成员当前同本成绩方向未知，不能视为受益或零分；朋友互助也可属于协助，二者不互斥。缺动机与钥匙证据，保持未知。\"\n      },\n      {\n        \"id\": \"routine_group\",\n        \"assessment\": \"possible\",\n        \"evidence_ids\": [\"investigation:team:20827902\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"多场已验证五人身份且同一四人稳定组合共同出现，符合固定班子日常打本或朋友互助的特征。但稳定组合不等于真实核心身份，动机未知，也不能排除其中夹带协助，因此评估为可能而非支持。\"\n      },\n      {\n        \"id\": \"later_progress\",\n        \"assessment\": \"supported\",\n        \"evidence_ids\": [\"investigation:own:20827902\", \"investigation:own:20828794\", \"investigation:motive_unknown:20827902\"],\n        \"counter_evidence_ids\": [\"behavior:prior_higher:21239083\"],\n        \"explanation\": \"两场低层通关均低于本人当前同本成绩代理，可由其后的进步解释。反证是更晚的一场在本人已达成更高层之后仍以低层进行，说明差距不能全部归于后续进步，仍存在协作等其他解释；但此前两场的差距确实已由跑前更高成绩覆盖，综合仍支持该假设部分成立。\"\n      }\n    ],\n    \"next_question_ids\": [\n      \"scored_history:0fce9b7d75511971778a8f5136eb842538008db0df7c50fb63b2c437f8ac3bc8:14063\",\n      \"scored_runs:1ced9beb0acf73581727f824479fc85a17dc3201e51dd5a7f57166ffa2371882\",\n      \"scored_runs:305cb96982109ccbc1af52b4afda99f2f508cd465e2ce5dd050d3a0ba5230bb7\"\n    ]\n  }\n}",
    "evidence": {
      "eligible_labels": [
        {
          "code": "recent_inquiry",
          "label": "近期协作线索（待解释）",
          "confidence_max": "低",
          "evidence_ids": [
            "investigation:own:20827902",
            "investigation:team:20827902",
            "investigation:own:20828794"
          ],
          "reason_options": {
            "recent_clue": "已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。"
          },
          "limitation_options": {
            "current_not_history": "当前成绩仅为代理，不代表历史当场加分。",
            "unknown_motive": "协作、练习和后续进步仍可能；无法确认钥匙归属或交易。"
          },
          "required_limitation_ids": [
            "current_not_history",
            "unknown_motive"
          ]
        }
      ],
      "evidence": [
        {
          "id": "timed_experience"
        },
        {
          "id": "multiple_specs"
        },
        {
          "id": "behavior:lead:20827902"
        },
        {
          "id": "behavior:lead:20828794"
        },
        {
          "id": "behavior:joint_progress:21021396"
        },
        {
          "id": "behavior:prior_higher:21239083"
        },
        {
          "id": "behavior:lead:21239083"
        },
        {
          "id": "behavior:key_recovery_unknown:21239083"
        },
        {
          "id": "rotating_core"
        },
        {
          "id": "investigation:own:20827902"
        },
        {
          "id": "investigation:team:20827902"
        },
        {
          "id": "investigation:unknown:20827902"
        },
        {
          "id": "investigation:unknown:group1:20827902"
        },
        {
          "id": "investigation:unknown:group2:20827902"
        },
        {
          "id": "investigation:unknown:group3:20827902"
        },
        {
          "id": "investigation:unknown:group4:20827902"
        },
        {
          "id": "investigation:group_unknown:20827902"
        },
        {
          "id": "investigation:core_unknown:20827902"
        },
        {
          "id": "investigation:motive_unknown:20827902"
        },
        {
          "id": "investigation:own:20828794"
        },
        {
          "id": "investigation:team:20828794"
        },
        {
          "id": "investigation:unknown:20828794"
        },
        {
          "id": "investigation:unknown:group1:20828794"
        },
        {
          "id": "investigation:unknown:group2:20828794"
        },
        {
          "id": "investigation:unknown:group3:20828794"
        },
        {
          "id": "investigation:unknown:group4:20828794"
        },
        {
          "id": "investigation:group_unknown:20828794"
        },
        {
          "id": "investigation:core_unknown:20828794"
        },
        {
          "id": "investigation:motive_unknown:20828794"
        },
        {
          "id": "investigation:own:21239083"
        },
        {
          "id": "investigation:team:21239083"
        },
        {
          "id": "investigation:unknown:21239083"
        },
        {
          "id": "investigation:unknown:group1:21239083"
        },
        {
          "id": "investigation:unknown:group2:21239083"
        },
        {
          "id": "investigation:unknown:group3:21239083"
        },
        {
          "id": "investigation:unknown:group4:21239083"
        },
        {
          "id": "investigation:group_unknown:21239083"
        },
        {
          "id": "investigation:core_unknown:21239083"
        },
        {
          "id": "investigation:motive_unknown:21239083"
        },
        {
          "id": "behavior:interpretation_limits"
        }
      ],
      "metrics": {
        "investigation": {
          "triggered": true,
          "hypotheses": [
            {
              "id": "key_access",
              "label": "钥匙协调线索",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:motive_unknown:20827902",
                "investigation:motive_unknown:20828794",
                "investigation:motive_unknown:21239083"
              ]
            },
            {
              "id": "assistance",
              "label": "疑似协助他人上分",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:unknown:20827902",
                "investigation:unknown:group1:20827902",
                "investigation:unknown:group2:20827902",
                "investigation:unknown:group3:20827902",
                "investigation:unknown:group4:20827902",
                "investigation:group_unknown:20827902",
                "investigation:core_unknown:20827902",
                "investigation:unknown:20828794",
                "investigation:unknown:group1:20828794",
                "investigation:unknown:group2:20828794",
                "investigation:unknown:group3:20828794",
                "investigation:unknown:group4:20828794",
                "investigation:group_unknown:20828794",
                "investigation:core_unknown:20828794",
                "investigation:unknown:21239083",
                "investigation:unknown:group1:21239083",
                "investigation:unknown:group2:21239083",
                "investigation:unknown:group3:21239083",
                "investigation:unknown:group4:21239083",
                "investigation:group_unknown:21239083",
                "investigation:core_unknown:21239083",
                "investigation:motive_unknown:20827902",
                "investigation:motive_unknown:20828794",
                "investigation:motive_unknown:21239083",
                "behavior:key_recovery_unknown:21239083"
              ]
            },
            {
              "id": "routine_group",
              "label": "日常协作、朋友互助或练习",
              "status": "compatible",
              "support_ids": [
                "investigation:team:20827902",
                "investigation:team:20828794",
                "investigation:team:21239083"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "investigation:motive_unknown:20827902",
                "investigation:motive_unknown:20828794",
                "investigation:motive_unknown:21239083"
              ]
            },
            {
              "id": "later_progress",
              "label": "后续进步造成当前上限与近期通关差距",
              "status": "compatible",
              "support_ids": [
                "investigation:own:20827902",
                "investigation:own:20828794"
              ],
              "counter_ids": [
                "behavior:prior_higher:21239083"
              ],
              "unknown_ids": [
                "investigation:motive_unknown:20827902",
                "investigation:motive_unknown:20828794",
                "investigation:motive_unknown:21239083"
              ]
            }
          ],
          "questions": [
            {
              "id": "scored_history:0fce9b7d75511971778a8f5136eb842538008db0df7c50fb63b2c437f8ac3bc8:14063"
            },
            {
              "id": "scored_history:0fce9b7d75511971778a8f5136eb842538008db0df7c50fb63b2c437f8ac3bc8:16091"
            },
            {
              "id": "scored_history:0fce9b7d75511971778a8f5136eb842538008db0df7c50fb63b2c437f8ac3bc8:9526"
            },
            {
              "id": "scored_history:0fce9b7d75511971778a8f5136eb842538008db0df7c50fb63b2c437f8ac3bc8:9527"
            },
            {
              "id": "scored_runs:1ced9beb0acf73581727f824479fc85a17dc3201e51dd5a7f57166ffa2371882"
            },
            {
              "id": "scored_runs:305cb96982109ccbc1af52b4afda99f2f508cd465e2ce5dd050d3a0ba5230bb7"
            },
            {
              "id": "scored_runs:91ec5abd5ac6253707f1cc63a7e2eed5e4ce3ee771a3386ea86aa7948d6b1831"
            }
          ]
        },
        "behavior_review": {
          "witnesses": {
            "carrying": [],
            "being_carried": [],
            "joint_progress": [
              21021396
            ],
            "key_access": [],
            "high_score_collaborator": []
          },
          "supported": {}
        }
      }
    },
    "reject": true
  },
  {
    "raw": "{\n  \"observations\": [\n    {\n      \"code\": \"carrying\",\n      \"confidence\": \"低\",\n      \"reason_id\": \"repeated_witnesses\",\n      \"limitation_ids\": [\"sample_scope\", \"unknown_motive\"],\n      \"evidence_ids\": [\"behavior:carrying:21238381\", \"behavior:carrying:21238504\", \"behavior:carrying:21238410\"]\n    },\n    {\n      \"code\": \"recent_inquiry\",\n      \"confidence\": \"低\",\n      \"reason_id\": \"recent_clue\",\n      \"limitation_ids\": [\"current_not_history\", \"unknown_motive\"],\n      \"evidence_ids\": [\"investigation:own:21238381\", \"investigation:team:21238381\", \"investigation:own:21238403\"]\n    }\n  ],\n  \"conclusion_id\": \"public_sample\",\n  \"investigation\": {\n    \"summary\": \"同一五人组合在短期内多次共同限时二十层，其中至少三场开打前本人已有同本更高限时，说明这些场次并非为本人当场上分；协助他人上分的低置信线索因此成立。日常协作、朋友互助或练习同样兼容，钥匙归属与动机未知。后续进步解释被多场跑前更高记录削弱。整体仍为低置信比较，不据此定性交易或带人。\",\n    \"confidence\": \"低\",\n    \"hypotheses\": [\n      {\n        \"id\": \"assistance\",\n        \"assessment\": \"supported\",\n        \"evidence_ids\": [\"behavior:carrying:21238381\", \"behavior:carrying:21238504\", \"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"至少三场独立通关中，本人开打前已有同本更高限时且多本达标，同伴快照低于本人跑前记录，成绩时序与成员关系支持协助上分的低置信解释。但朋友互助本身也可属于协助，样本有限，钥匙归属与收费未知，故仍为低置信，不证明交易。\"\n      },\n      {\n        \"id\": \"routine_group\",\n        \"assessment\": \"possible\",\n        \"evidence_ids\": [\"investigation:team:21238381\", \"investigation:team:21238403\", \"investigation:motive_unknown:21238381\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"已验证五人稳定组合多场共同在场，与朋友互助或练习的日常协作解释兼容。该解释与协助上分并非互斥：协助可以发生在固定朋友组合内部。动机与钥匙归属未知，无法在两者间裁定，仅能并列为候选。\"\n      },\n      {\n        \"id\": \"later_progress\",\n        \"assessment\": \"weakened\",\n        \"evidence_ids\": [\"investigation:motive_unknown:21238381\"],\n        \"counter_evidence_ids\": [\"behavior:prior_higher:21238381\", \"behavior:prior_higher:21238504\", \"behavior:prior_higher:21238410\", \"behavior:prior_higher:21238497\", \"behavior:prior_higher:21238494\", \"behavior:prior_higher:21238403\"],\n        \"explanation\": \"若低层场次源于本场之后才达到更高上限，则无需额外解释。但多场开打前已有同本更高限时，时序上排除了以后续进步解释这些场次本人无收益的方向，该假设因此被反证削弱，尽管部分场次证据仍不完整。\"\n      },\n      {\n        \"id\": \"key_access\",\n        \"assessment\": \"unknown\",\n        \"evidence_ids\": [\"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"钥匙归属无法从公开通关记录判定：谁持有钥匙、是否旧钥或他人钥匙均未知。该假设既无支持也无反证，不能确认，也不能排除；在比较协助与日常协作时保持未知，不作定性。\"\n      }\n    ],\n    \"next_question_ids\": [\n      \"scored_history:a128570606e5ba985c0f4436b3030c7127f9df3573677040c74bea1e9844b758:16359\",\n      \"scored_runs:adbdea248f711a6f287db8ad070ab3a31dcf34f787acb1c3a84cb7cad82af417\",\n      \"scored_runs:dc4847ef71a90b0e727a6b30c895b1d231101e92ec57af34f6af6bc9f662f579\"\n    ]\n  }\n}",
    "evidence": {
      "eligible_labels": [
        {
          "code": "carrying",
          "label": "疑似协助他人上分",
          "confidence_max": "低",
          "evidence_ids": [
            "behavior:carrying:21238381",
            "behavior:carrying:21238504",
            "behavior:carrying:21238410"
          ],
          "reason_options": {
            "repeated_witnesses": "至少两场独立通关的成绩时序与成员关系支持这一低置信解释。"
          },
          "limitation_options": {
            "sample_scope": "只代表已观测样本，不证明长期身份或完整历史。",
            "unknown_motive": "朋友互助可属于协助上分；钥匙归属及收费未知。"
          },
          "required_limitation_ids": [
            "sample_scope",
            "unknown_motive"
          ]
        },
        {
          "code": "recent_inquiry",
          "label": "近期协作线索（待解释）",
          "confidence_max": "低",
          "evidence_ids": [
            "investigation:own:21238381",
            "investigation:team:21238381",
            "investigation:own:21238403"
          ],
          "reason_options": {
            "recent_clue": "已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。"
          },
          "limitation_options": {
            "current_not_history": "当前成绩仅为代理，不代表历史当场加分。",
            "unknown_motive": "协作、练习和后续进步仍可能；无法确认钥匙归属或交易。"
          },
          "required_limitation_ids": [
            "current_not_history",
            "unknown_motive"
          ]
        }
      ],
      "evidence": [
        {
          "id": "timed_experience"
        },
        {
          "id": "behavior:prior_higher:21238381"
        },
        {
          "id": "behavior:lead:21238381"
        },
        {
          "id": "behavior:carrying:21238381"
        },
        {
          "id": "behavior:prior_higher:21238504"
        },
        {
          "id": "behavior:lead:21238504"
        },
        {
          "id": "behavior:carrying:21238504"
        },
        {
          "id": "behavior:prior_higher:21238410"
        },
        {
          "id": "behavior:lead:21238410"
        },
        {
          "id": "behavior:carrying:21238410"
        },
        {
          "id": "behavior:prior_higher:21238497"
        },
        {
          "id": "behavior:lead:21238497"
        },
        {
          "id": "behavior:carrying:21238497"
        },
        {
          "id": "behavior:prior_higher:21238494"
        },
        {
          "id": "behavior:lead:21238494"
        },
        {
          "id": "behavior:carrying:21238494"
        },
        {
          "id": "behavior:prior_higher:21238403"
        },
        {
          "id": "behavior:lead:21238403"
        },
        {
          "id": "behavior:carrying:21238403"
        },
        {
          "id": "investigation:own:21238381"
        },
        {
          "id": "investigation:team:21238381"
        },
        {
          "id": "investigation:unknown:group0:21238381"
        },
        {
          "id": "investigation:unknown:group1:21238381"
        },
        {
          "id": "investigation:unknown:group2:21238381"
        },
        {
          "id": "investigation:unknown:group3:21238381"
        },
        {
          "id": "investigation:unknown:group4:21238381"
        },
        {
          "id": "investigation:group_unknown:21238381"
        },
        {
          "id": "investigation:core_unknown:21238381"
        },
        {
          "id": "investigation:motive_unknown:21238381"
        },
        {
          "id": "investigation:own:21238403"
        },
        {
          "id": "investigation:team:21238403"
        },
        {
          "id": "investigation:unknown:group0:21238403"
        },
        {
          "id": "investigation:unknown:group1:21238403"
        },
        {
          "id": "investigation:unknown:group2:21238403"
        },
        {
          "id": "investigation:unknown:group3:21238403"
        },
        {
          "id": "investigation:unknown:group4:21238403"
        },
        {
          "id": "investigation:group_unknown:21238403"
        },
        {
          "id": "investigation:core_unknown:21238403"
        },
        {
          "id": "investigation:motive_unknown:21238403"
        },
        {
          "id": "investigation:own:21238410"
        },
        {
          "id": "investigation:team:21238410"
        },
        {
          "id": "investigation:unknown:group0:21238410"
        },
        {
          "id": "investigation:unknown:group1:21238410"
        },
        {
          "id": "investigation:unknown:group2:21238410"
        },
        {
          "id": "investigation:unknown:group3:21238410"
        },
        {
          "id": "investigation:unknown:group4:21238410"
        },
        {
          "id": "investigation:group_unknown:21238410"
        },
        {
          "id": "investigation:core_unknown:21238410"
        },
        {
          "id": "investigation:motive_unknown:21238410"
        },
        {
          "id": "investigation:own:21238494"
        },
        {
          "id": "investigation:team:21238494"
        },
        {
          "id": "investigation:unknown:group0:21238494"
        },
        {
          "id": "investigation:unknown:group1:21238494"
        },
        {
          "id": "investigation:unknown:group2:21238494"
        },
        {
          "id": "investigation:unknown:group3:21238494"
        },
        {
          "id": "investigation:unknown:group4:21238494"
        },
        {
          "id": "investigation:group_unknown:21238494"
        },
        {
          "id": "investigation:core_unknown:21238494"
        },
        {
          "id": "investigation:motive_unknown:21238494"
        },
        {
          "id": "investigation:own:21238497"
        },
        {
          "id": "investigation:team:21238497"
        },
        {
          "id": "investigation:unknown:group0:21238497"
        },
        {
          "id": "investigation:unknown:group1:21238497"
        },
        {
          "id": "investigation:unknown:group2:21238497"
        },
        {
          "id": "investigation:unknown:group3:21238497"
        },
        {
          "id": "investigation:unknown:group4:21238497"
        },
        {
          "id": "investigation:group_unknown:21238497"
        },
        {
          "id": "investigation:core_unknown:21238497"
        },
        {
          "id": "investigation:motive_unknown:21238497"
        },
        {
          "id": "investigation:own:21238504"
        },
        {
          "id": "investigation:team:21238504"
        },
        {
          "id": "investigation:unknown:group0:21238504"
        },
        {
          "id": "investigation:unknown:group1:21238504"
        },
        {
          "id": "investigation:unknown:group2:21238504"
        },
        {
          "id": "investigation:unknown:group3:21238504"
        },
        {
          "id": "investigation:unknown:group4:21238504"
        },
        {
          "id": "investigation:group_unknown:21238504"
        },
        {
          "id": "investigation:core_unknown:21238504"
        },
        {
          "id": "investigation:motive_unknown:21238504"
        },
        {
          "id": "behavior:interpretation_limits"
        }
      ],
      "metrics": {
        "investigation": {
          "triggered": true,
          "hypotheses": [
            {
              "id": "key_access",
              "label": "钥匙协调线索",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:motive_unknown:21238381",
                "investigation:motive_unknown:21238403",
                "investigation:motive_unknown:21238410",
                "investigation:motive_unknown:21238494",
                "investigation:motive_unknown:21238497",
                "investigation:motive_unknown:21238504"
              ]
            },
            {
              "id": "assistance",
              "label": "疑似协助他人上分",
              "status": "supported",
              "support_ids": [
                "behavior:carrying:21238381",
                "behavior:carrying:21238504",
                "behavior:carrying:21238410"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:unknown:group0:21238381",
                "investigation:unknown:group1:21238381",
                "investigation:unknown:group2:21238381",
                "investigation:unknown:group3:21238381",
                "investigation:unknown:group4:21238381",
                "investigation:group_unknown:21238381",
                "investigation:core_unknown:21238381",
                "investigation:unknown:group0:21238403",
                "investigation:unknown:group1:21238403",
                "investigation:unknown:group2:21238403",
                "investigation:unknown:group3:21238403",
                "investigation:unknown:group4:21238403",
                "investigation:group_unknown:21238403",
                "investigation:core_unknown:21238403",
                "investigation:unknown:group0:21238410",
                "investigation:unknown:group1:21238410",
                "investigation:unknown:group2:21238410",
                "investigation:unknown:group3:21238410",
                "investigation:unknown:group4:21238410",
                "investigation:group_unknown:21238410",
                "investigation:core_unknown:21238410",
                "investigation:unknown:group0:21238494",
                "investigation:unknown:group1:21238494",
                "investigation:unknown:group2:21238494",
                "investigation:unknown:group3:21238494",
                "investigation:unknown:group4:21238494",
                "investigation:group_unknown:21238494",
                "investigation:core_unknown:21238494",
                "investigation:unknown:group0:21238497",
                "investigation:unknown:group1:21238497",
                "investigation:unknown:group2:21238497",
                "investigation:unknown:group3:21238497",
                "investigation:unknown:group4:21238497",
                "investigation:group_unknown:21238497",
                "investigation:core_unknown:21238497",
                "investigation:unknown:group0:21238504",
                "investigation:unknown:group1:21238504",
                "investigation:unknown:group2:21238504",
                "investigation:unknown:group3:21238504",
                "investigation:unknown:group4:21238504",
                "investigation:group_unknown:21238504",
                "investigation:core_unknown:21238504",
                "investigation:motive_unknown:21238381",
                "investigation:motive_unknown:21238403",
                "investigation:motive_unknown:21238410",
                "investigation:motive_unknown:21238494",
                "investigation:motive_unknown:21238497",
                "investigation:motive_unknown:21238504"
              ]
            },
            {
              "id": "routine_group",
              "label": "日常协作、朋友互助或练习",
              "status": "compatible",
              "support_ids": [
                "investigation:team:21238381",
                "investigation:team:21238403",
                "investigation:team:21238410",
                "investigation:team:21238494",
                "investigation:team:21238497",
                "investigation:team:21238504"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "investigation:motive_unknown:21238381",
                "investigation:motive_unknown:21238403",
                "investigation:motive_unknown:21238410",
                "investigation:motive_unknown:21238494",
                "investigation:motive_unknown:21238497",
                "investigation:motive_unknown:21238504"
              ]
            },
            {
              "id": "later_progress",
              "label": "后续进步造成当前上限与近期通关差距",
              "status": "weakened",
              "support_ids": [],
              "counter_ids": [
                "behavior:prior_higher:21238381",
                "behavior:prior_higher:21238504",
                "behavior:prior_higher:21238410",
                "behavior:prior_higher:21238497",
                "behavior:prior_higher:21238494",
                "behavior:prior_higher:21238403"
              ],
              "unknown_ids": [
                "investigation:motive_unknown:21238381",
                "investigation:motive_unknown:21238403",
                "investigation:motive_unknown:21238410",
                "investigation:motive_unknown:21238494",
                "investigation:motive_unknown:21238497",
                "investigation:motive_unknown:21238504"
              ]
            }
          ],
          "questions": [
            {
              "id": "scored_history:a128570606e5ba985c0f4436b3030c7127f9df3573677040c74bea1e9844b758:16359"
            },
            {
              "id": "scored_history:a128570606e5ba985c0f4436b3030c7127f9df3573677040c74bea1e9844b758:16865"
            },
            {
              "id": "scored_history:a128570606e5ba985c0f4436b3030c7127f9df3573677040c74bea1e9844b758:9526"
            },
            {
              "id": "scored_history:a128570606e5ba985c0f4436b3030c7127f9df3573677040c74bea1e9844b758:9527"
            },
            {
              "id": "scored_runs:adbdea248f711a6f287db8ad070ab3a31dcf34f787acb1c3a84cb7cad82af417"
            },
            {
              "id": "scored_runs:dc4847ef71a90b0e727a6b30c895b1d231101e92ec57af34f6af6bc9f662f579"
            }
          ]
        },
        "behavior_review": {
          "witnesses": {
            "carrying": [
              21238381,
              21238403,
              21238410,
              21238494,
              21238497,
              21238504
            ],
            "being_carried": [],
            "joint_progress": [],
            "key_access": [],
            "high_score_collaborator": []
          },
          "supported": {
            "carrying": [
              "behavior:carrying:21238381",
              "behavior:carrying:21238504",
              "behavior:carrying:21238410",
              "behavior:carrying:21238497",
              "behavior:carrying:21238494",
              "behavior:carrying:21238403"
            ]
          }
        }
      }
    },
    "reject": true
  },
  {
    "raw": "{\n  \"observations\": [\n    {\n      \"code\": \"joint_progress\",\n      \"confidence\": \"低\",\n      \"reason_id\": \"repeated_witnesses\",\n      \"limitation_ids\": [\"sample_scope\", \"unknown_motive\"],\n      \"evidence_ids\": [\"behavior:joint_progress:20822224\", \"behavior:joint_progress:20994618\", \"behavior:joint_progress:21002420\"]\n    },\n    {\n      \"code\": \"recent_inquiry\",\n      \"confidence\": \"低\",\n      \"reason_id\": \"recent_clue\",\n      \"limitation_ids\": [\"current_not_history\", \"unknown_motive\"],\n      \"evidence_ids\": [\"investigation:own:20811458\", \"investigation:team:20811458\", \"investigation:own:20986869\"]\n    }\n  ],\n  \"conclusion_id\": \"public_sample\",\n  \"investigation\": {\n    \"summary\": \"最近公开样本中同一五人组合多次共同出现，至少数场呈现包含本人及至少三名同伴的同本共同升层，本人部分场次低于自己当前同本代理成绩。共同升层为已支持的低置信事实候选，但协作、协助、日常朋友互助与钥匙协调均未排除；钥匙归属、动机与收费未知，不据此定性。\",\n    \"confidence\": \"低\",\n    \"hypotheses\": [\n      {\n        \"id\": \"joint_progress\",\n        \"assessment\": \"supported\",\n        \"evidence_ids\": [\"behavior:joint_progress:20822224\", \"behavior:joint_progress:20994618\", \"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"多场记录显示包含本人与至少三名同伴的同本共同前场升层，时序与成员关系支持共同提高这一低置信解释，且无反证。但共同记录提高不等于全员实际净增分，也不证明动机；当前快照非历史加分，朋友互助可同时属于协助上分，钥匙归属与收费未知。\"\n      },\n      {\n        \"id\": \"routine_group\",\n        \"assessment\": \"possible\",\n        \"evidence_ids\": [\"investigation:team:20811458\", \"investigation:team:20986869\", \"investigation:motive_unknown:20811458\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"已验证场次中五人稳定组合均在场，结构本身与日常协作、朋友互助或练习相容。无反证削弱该解释，但动机、钥匙归属与交易事实未知，稳定五人不证明带人，也不能排除其他候选解释。\"\n      },\n      {\n        \"id\": \"assistance\",\n        \"assessment\": \"unknown\",\n        \"evidence_ids\": [\"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"各竞争分组的组外稳定成员同本成绩方向未知，不能判为零或反证；真实核心与外援身份不明，本人线索尚无法推广到稳定成员。因此疑似协助他人上分既无支持也无反证，朋友互助亦可属于协助，钥匙归属与收费未知。\"\n      },\n      {\n        \"id\": \"key_access\",\n        \"assessment\": \"unknown\",\n        \"evidence_ids\": [\"behavior:interpretation_limits\"],\n        \"counter_evidence_ids\": [],\n        \"explanation\": \"公开通关与当前计分不含钥匙归属、背包库存或交易事实；已解锁也不证明本场钥匙归属，不能排除旧钥匙或他人钥匙。该假设无支持证据，也无反证，保持未知，不作商业或交易定性。\"\n      }\n    ],\n    \"next_question_ids\": [\n      \"scored_history:50bdb498ac6490b15a61899d45dc95c40b246df4b91ec6852e7226d9f07ec8de:9526\",\n      \"scored_runs:50bdb498ac6490b15a61899d45dc95c40b246df4b91ec6852e7226d9f07ec8de\",\n      \"scored_runs:2e79b7461b856cfe68cac4504e6bdd07e789bf9bc0f03a6d6a6edc3124fbf67d\"\n    ]\n  }\n}",
    "evidence": {
      "eligible_labels": [
        {
          "code": "joint_progress",
          "label": "共同提高成绩线索",
          "confidence_max": "低",
          "evidence_ids": [
            "behavior:joint_progress:20822224",
            "behavior:joint_progress:20994618",
            "behavior:joint_progress:21002420"
          ],
          "reason_options": {
            "repeated_witnesses": "至少两场独立通关的成绩时序与成员关系支持这一低置信解释。"
          },
          "limitation_options": {
            "sample_scope": "只代表已观测样本，不证明长期身份或完整历史。",
            "unknown_motive": "朋友互助可属于协助上分；钥匙归属及收费未知。"
          },
          "required_limitation_ids": [
            "sample_scope",
            "unknown_motive"
          ]
        },
        {
          "code": "recent_inquiry",
          "label": "近期协作线索（待解释）",
          "confidence_max": "低",
          "evidence_ids": [
            "investigation:own:20811458",
            "investigation:team:20811458",
            "investigation:own:20986869"
          ],
          "reason_options": {
            "recent_clue": "已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。"
          },
          "limitation_options": {
            "current_not_history": "当前成绩仅为代理，不代表历史当场加分。",
            "unknown_motive": "协作、练习和后续进步仍可能；无法确认钥匙归属或交易。"
          },
          "required_limitation_ids": [
            "current_not_history",
            "unknown_motive"
          ]
        }
      ],
      "evidence": [
        {
          "id": "timed_experience"
        },
        {
          "id": "behavior:lead:20811458"
        },
        {
          "id": "behavior:joint_progress:20822224"
        },
        {
          "id": "behavior:lead:20986869"
        },
        {
          "id": "behavior:lead:20994618"
        },
        {
          "id": "behavior:joint_progress:20994618"
        },
        {
          "id": "behavior:joint_progress:21002420"
        },
        {
          "id": "behavior:joint_progress:21347946"
        },
        {
          "id": "fixed_team"
        },
        {
          "id": "investigation:own:20811458"
        },
        {
          "id": "investigation:team:20811458"
        },
        {
          "id": "investigation:unknown:group0:20811458"
        },
        {
          "id": "investigation:unknown:group1:20811458"
        },
        {
          "id": "investigation:unknown:group2:20811458"
        },
        {
          "id": "investigation:unknown:group3:20811458"
        },
        {
          "id": "investigation:unknown:group4:20811458"
        },
        {
          "id": "investigation:group_unknown:20811458"
        },
        {
          "id": "investigation:core_unknown:20811458"
        },
        {
          "id": "investigation:motive_unknown:20811458"
        },
        {
          "id": "investigation:own:20986869"
        },
        {
          "id": "investigation:team:20986869"
        },
        {
          "id": "investigation:unknown:group0:20986869"
        },
        {
          "id": "investigation:unknown:group1:20986869"
        },
        {
          "id": "investigation:unknown:group2:20986869"
        },
        {
          "id": "investigation:unknown:group3:20986869"
        },
        {
          "id": "investigation:unknown:group4:20986869"
        },
        {
          "id": "investigation:group_unknown:20986869"
        },
        {
          "id": "investigation:core_unknown:20986869"
        },
        {
          "id": "investigation:motive_unknown:20986869"
        },
        {
          "id": "investigation:own:20994618"
        },
        {
          "id": "investigation:team:20994618"
        },
        {
          "id": "investigation:unknown:group0:20994618"
        },
        {
          "id": "investigation:unknown:group1:20994618"
        },
        {
          "id": "investigation:unknown:group2:20994618"
        },
        {
          "id": "investigation:unknown:group3:20994618"
        },
        {
          "id": "investigation:unknown:group4:20994618"
        },
        {
          "id": "investigation:group_unknown:20994618"
        },
        {
          "id": "investigation:core_unknown:20994618"
        },
        {
          "id": "investigation:motive_unknown:20994618"
        },
        {
          "id": "behavior:interpretation_limits"
        }
      ],
      "metrics": {
        "investigation": {
          "triggered": true,
          "hypotheses": [
            {
              "id": "key_access",
              "label": "钥匙协调线索",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:motive_unknown:20811458",
                "investigation:motive_unknown:20986869",
                "investigation:motive_unknown:20994618"
              ]
            },
            {
              "id": "joint_progress",
              "label": "共同提高成绩线索",
              "status": "supported",
              "support_ids": [
                "behavior:joint_progress:20822224",
                "behavior:joint_progress:20994618",
                "behavior:joint_progress:21002420"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits"
              ]
            },
            {
              "id": "assistance",
              "label": "疑似协助他人上分",
              "status": "unknown",
              "support_ids": [],
              "counter_ids": [],
              "unknown_ids": [
                "behavior:interpretation_limits",
                "investigation:unknown:group0:20811458",
                "investigation:unknown:group1:20811458",
                "investigation:unknown:group2:20811458",
                "investigation:unknown:group3:20811458",
                "investigation:unknown:group4:20811458",
                "investigation:group_unknown:20811458",
                "investigation:core_unknown:20811458",
                "investigation:unknown:group0:20986869",
                "investigation:unknown:group1:20986869",
                "investigation:unknown:group2:20986869",
                "investigation:unknown:group3:20986869",
                "investigation:unknown:group4:20986869",
                "investigation:group_unknown:20986869",
                "investigation:core_unknown:20986869",
                "investigation:unknown:group0:20994618",
                "investigation:unknown:group1:20994618",
                "investigation:unknown:group2:20994618",
                "investigation:unknown:group3:20994618",
                "investigation:unknown:group4:20994618",
                "investigation:group_unknown:20994618",
                "investigation:core_unknown:20994618",
                "investigation:motive_unknown:20811458",
                "investigation:motive_unknown:20986869",
                "investigation:motive_unknown:20994618"
              ]
            },
            {
              "id": "routine_group",
              "label": "日常协作、朋友互助或练习",
              "status": "compatible",
              "support_ids": [
                "investigation:team:20811458",
                "investigation:team:20986869",
                "investigation:team:20994618"
              ],
              "counter_ids": [],
              "unknown_ids": [
                "investigation:motive_unknown:20811458",
                "investigation:motive_unknown:20986869",
                "investigation:motive_unknown:20994618"
              ]
            }
          ],
          "questions": [
            {
              "id": "scored_history:50bdb498ac6490b15a61899d45dc95c40b246df4b91ec6852e7226d9f07ec8de:9526"
            },
            {
              "id": "scored_runs:50bdb498ac6490b15a61899d45dc95c40b246df4b91ec6852e7226d9f07ec8de"
            },
            {
              "id": "scored_runs:2e79b7461b856cfe68cac4504e6bdd07e789bf9bc0f03a6d6a6edc3124fbf67d"
            },
            {
              "id": "scored_runs:7163c59093dff1026edbebccca7a49fb580b841cc68cb41881f5e49efb900154"
            },
            {
              "id": "scored_runs:9160d1ae1065e6bb7f1af66a860c9abc6f9ce1b0cb070dfc981b3712c9ffdeaa"
            },
            {
              "id": "scored_runs:e2975e136caca70bfb76d20876580cf44ca5dc7efab940b2a56093b96c4a8959"
            }
          ]
        },
        "behavior_review": {
          "witnesses": {
            "carrying": [],
            "being_carried": [],
            "joint_progress": [
              20822224,
              20994618,
              21002420,
              21347946
            ],
            "key_access": [],
            "high_score_collaborator": []
          },
          "supported": {
            "joint_progress": [
              "behavior:joint_progress:20822224",
              "behavior:joint_progress:20994618",
              "behavior:joint_progress:21002420",
              "behavior:joint_progress:21347946"
            ]
          }
        }
      }
    },
    "reject": false
  }
]''')
