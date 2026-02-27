CREATE UNIQUE INDEX idx_tt_ai_documents_job_id
  ON tt_ai_documents(job_id)
  WHERE job_id IS NOT NULL;

CREATE INDEX idx_tt_ai_documents_userID
  ON tt_ai_documents("userID");

CREATE INDEX idx_tt_ai_documents_status
  ON tt_ai_documents(status);

CREATE INDEX idx_tt_ai_documents_vehicle_make_id
  ON tt_ai_documents(vehicle_make_id);

CREATE INDEX idx_tt_ai_documents_vehicle_model_id
  ON tt_ai_documents(vehicle_model_id);

CREATE INDEX idx_tt_ai_documents_created_at
  ON tt_ai_documents(created_at DESC);

CREATE INDEX idx_tt_ai_chunks_document_id
  ON tt_ai_chunks(document_id);

CREATE INDEX idx_tt_ai_chunks_document_chunk
  ON tt_ai_chunks(document_id, chunk_index);