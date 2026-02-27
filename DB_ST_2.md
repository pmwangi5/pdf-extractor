NOTE: TABLES AND FIELDS NAMED EXACTLY AS BELOW


table: tt_ai_documents


id- uuid, primary key, unique, default: gen_random_uuid()


source- text, nullable


title- text


created_at- timestamp with time zone, default: now()


userID- uuid, nullable


vehicle_make- text, nullable - (infered vehicle make from pdf)


vehicle_make_id- uuid, nullable - (get id for vehicle where vehicle_make like graphql mycar_vehicle_makes.make)


vehicle_model- text, nullable - (infered vehicle model from pdf)


vehicle_model_id- uuid, nullable - (get id for vehicle where vehicle_model like graphql mycar_vehicle_models.name)


filename- text, nullable


preview_url- text, nullable


num_pages- integer, nullable


metadata- jsonb, nullable


status- text, nullable, default: 'processing'::text


upload_device- text, nullable


job_id- text, nullable 

#############################
table: tt_ai_chunks


id- uuid, primary key, unique, default: gen_random_uuid()


document_id- uuid


page- integer, nullable


content- text


embedding_chatgpt- vector, nullable (sql setup is vector(1536))


chatgpt_model_name- text, nullable


embedding_mistral- vector, nullable (sql setup is vector(1536))


mistral_model_name- text, nullable


created_at- timestamp with time zone, nullable, default: now()


chunk_index- integer


printed_page- text, nullable


chapter- text, nullable


char_count- integer 