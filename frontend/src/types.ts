export interface ColumnMeta { kind: 'dimension'|'money'|'ratio'|'count'|'number'; unit: string; precision: number|null; source_type?:string; metric_id?: string; metric_version?: string }
export interface SchemaField { name: string; label: string; type: string; description?: string; sensitive?: boolean }
export interface SchemaTable { id: string; name?: string; label: string; description: string; fields: SchemaField[]; database?: string }
export interface ClarificationOption { id: string; label: string; description: string; recommended: boolean }
export interface VisualizationSpec { type: 'bar'|'pie'|'line'; title: string; source_task_id: string; category_field: string; value_field: string; max_items: number }
export interface AnalysisReport { title: string; markdown: string; visualizations: VisualizationSpec[]; sources: { task_id: string; title: string; sql: string; data_version: string; data_as_of: string; metric_version?: string; result_fingerprint: string }[]; facts: Record<string,unknown>[]; completeness: string }
export interface QueryResult {
 task_id: string; status: 'waiting_clarification'|'completed'|'failed'; route: 'database_query'|'data_qa'|'direct_response'; message: string;
 interpretation: null|{metric:string;dimension:string;time_range:string;table:string;assumptions:string[];parameter_sources:Record<string,string>;metric_definition:string;metric_version:string;data_as_of:string;scope_label:string};
 clarification: null|{parameter:string;question:string;reason:string;options:ClarificationOption[]};
 columns:string[];rows:Record<string,string|number|null>[];analysis:string|null;sql:string|null;steps:string[];saved:boolean;result_title?:string|null;
 column_metadata:Record<string,ColumnMeta>;truncated:boolean;returned_rows:number;total_rows:number|null;data_version:string;data_as_of:string;generated_at:string;result_fingerprint:string;
 query_plan:Record<string,unknown>|null;validation:Record<string,unknown>;comparison_stats:Record<string,unknown>[];report?:AnalysisReport|null;
 tool_calls?:{tool:string;success:boolean;reason?:string;error?:string}[];
 retrieval?:{hits?:{doc_id:string;field_label:string;score:number;source:string}[];selected_count:number;embedding_source:string;rerank_source:string;metric_knowledge?:Record<string,unknown>};
}
export interface ReportDataSource { taskId:string;title:string;columns:string[];rows:Record<string,string|number|null>[];column_metadata?:Record<string,ColumnMeta> }
export interface WorkspaceConfig { schema_fields?:{name:string;tableId?:string;aggregation?:'auto'}[];analysis_table_ids?:string[] }
export interface SavedMemory { id:string;kind:'result_table'|'schema_field';created_at:string;task_id?:string;title?:string;query?:string;columns?:string[];rows?:Record<string,string|number|null>[];column_metadata?:Record<string,ColumnMeta>;table_id?:string;name?:string;label?:string;field_type?:string }
export interface AuthUser { user_id:string;username:string;display_name:string;role:string }
export interface LoginResponse { access_token:string;token_type:string;user:AuthUser }
export interface MetricDefinition { id:string;label:string;definition:string;version:string;time_kind:string;unit:string;grain:string;numerator:string;denominator:string }
export interface AppConfig { scenario:string;credit_demo_mode:boolean;credit_reference_date:string;dataset:{data_as_of:string;data_version:string;synthetic:boolean;data_range:{start:string;end:string}};metrics:MetricDefinition[] }
