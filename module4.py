# python module4.py

import asyncio
import json
import os
import logging
import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

# Monkey patching first
import openai
def _mock_get_default_openai_client(*args, **kwargs):
    return None
openai.AsyncOpenAI._get_default_openai_client = _mock_get_default_openai_client
openai.OpenAI._get_default_openai_client = _mock_get_default_openai_client

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

from agents import Agent, GuardrailFunctionOutput, OutputGuardrail, Runner
from agents.run_context import RunContextWrapper
from agents.lifecycle import AgentHooks

load_dotenv()  # Load environment variables

# --- Setup Logging ---
def setup_logging(module_name):
    """Set up logging to both console and file."""
    # Create logs directory if it doesn't exist
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs_dir, exist_ok=True)
    
    # Create a timestamp for the log filename
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(logs_dir, f"{module_name}_{timestamp}.log")
    
    # Configure logging
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.INFO)
    
    # Clear any existing handlers
    if logger.handlers:
        logger.handlers = []
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.INFO)
    file_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
    
    return logger

# Initialize logger
logger = setup_logging("module4")

# --- Text Validation Functions ---
def sanitize_text(text: str) -> str:
    """Clean and validate text to prevent corruption."""
    if not isinstance(text, str):
        return str(text)
        
    # Remove any non-printable or control characters
    text = ''.join(char for char in text if char.isprintable() or char in ['\n', '\t', ' '])
    
    # Check for obvious corruption patterns (random Unicode characters, etc.)
    # This regex looks for clusters of non-English characters that might indicate corruption
    corruption_pattern = r'[\u0400-\u04FF\u0600-\u06FF\u0900-\u097F\u3040-\u309F\u30A0-\u30FF\u3130-\u318F\uAC00-\uD7AF]{3,}'
    
    # Replace corrupted sections with a note
    text = re.sub(corruption_pattern, '[corrupted text removed]', text)
    
    # Ensure the text doesn't exceed a reasonable size (50KB)
    max_length = 50000
    if len(text) > max_length:
        text = text[:max_length] + "...[text truncated due to length]"
    
    return text

# --- Custom Agent Hooks for Detailed Logging ---
class DetailedLoggingHooks(AgentHooks):
    def __init__(self, logger):
        self.logger = logger

    async def before_generate(
        self, agent: Agent, inputs: List[Dict[str, Any]], context: RunContextWrapper[Any]
    ):
        """Log details before LLM generation."""
        self.logger.info(f"===== API CALL: {agent.name} =====")
        self.logger.info(f"Inputs to {agent.name}: {json.dumps(inputs, indent=2)}")
        return inputs
    
    async def after_generate(
        self, agent: Agent, response: Any, context: RunContextWrapper[Any]
    ):
        """Log details after LLM generation."""
        self.logger.info(f"===== API RESPONSE: {agent.name} =====")
        # Format the response for better readability
        try:
            if hasattr(response, 'final_output'):
                # Handle different response types
                if hasattr(response.final_output, 'revision_request_content'):
                    response.final_output.revision_request_content = sanitize_text(response.final_output.revision_request_content)
                if hasattr(response.final_output, 'reasoning'):
                    response.final_output.reasoning = sanitize_text(response.final_output.reasoning)
                if hasattr(response.final_output, 'impact_assessment'):
                    response.final_output.impact_assessment = sanitize_text(response.final_output.impact_assessment)
                
                response_content = json.dumps(response.final_output, indent=2) 
                self.logger.info(f"Response from {agent.name}: {response_content}")
            else:
                self.logger.info(f"Response from {agent.name}: {str(response)}")
        except Exception as e:
            self.logger.info(f"Response from {agent.name}: {str(response)}")
            self.logger.info(f"Could not format response as JSON: {e}")
        return response

# Create logging hooks
logging_hooks = DetailedLoggingHooks(logger)

# --- Pydantic Models ---
class SuccessCriteria(BaseModel):
    criteria: str
    reasoning: str
    rating: int = Field(..., description="Rating of the criterion (1-10)")
    
    @field_validator('rating')
    def check_rating(cls, v):
        if not 1 <= v <= 10:
            raise ValueError('Rating must be between 1 and 10')
        return v

class PlanItem(BaseModel):
    item_title: str = Field(..., description="A concise title for this plan item.")
    item_description: str = Field(..., description="A description of this step in the plan.")

class PlanOutline(BaseModel):
    plan_title: str = Field(..., description="A title for the overall plan.")
    plan_description: str = Field(..., description="A brief summary of the plan approach")
    plan_items: list[PlanItem] = Field(..., description="A list of plan items.")
    reasoning: str = Field(..., description="Reasoning for why this plan is suitable.")
    rating: int = Field(..., description="Rating of the plan's suitability (1-10).")
    created_by: str = Field(..., description="The name of the agent that created this plan")

    @field_validator('plan_items')
    def check_plan_items(cls, v):
        if len(v) < 3:
            raise ValueError('Must provide at least three plan items')
        return v

    @field_validator('rating')
    def check_rating(cls, v):
        if not 1 <= v <= 10:
            raise ValueError('Rating must be between 1 and 10')
        return v

class EvalResult(BaseModel):
    result: str = Field(..., description="Either the word 'pass' or the word 'fail'.")
    reasoning: str = Field(..., description="The evaluator's reasoning")
    criteria: SuccessCriteria = Field(..., description="The success criterion being evaluated against")

    @field_validator('result')
    def check_result(cls, v):
        if v.lower() not in ["pass", "fail"]:
            raise ValueError("Result must be 'pass' or 'fail'")
        return v.lower()
    
    @field_validator('reasoning')
    def validate_reasoning(cls, v):
        """Validate and sanitize reasoning text."""
        return sanitize_text(v)

class Module3Output(BaseModel): # For loading the JSON from module 3
    goal: str
    selected_criteria: list[SuccessCriteria]
    selected_outline: PlanOutline  # Original outline
    expanded_outline: PlanOutline  # Expanded items
    evaluation_results: list[EvalResult] # List of results
    criteria_summary: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="Summary of pass/fail counts per criterion"
    )

class RevisionRequest(BaseModel):
    revision_request_content: str = Field(..., description="Specific requested revision.")
    reasoning: str = Field(..., description="Why this revision is necessary.")
    targeted_criteria: List[str] = Field(..., description="The criteria this revision addresses.")
    
    @field_validator('revision_request_content')
    def validate_request(cls, v):
        """Validate and sanitize revision request text."""
        return sanitize_text(v)
    
    @field_validator('reasoning')
    def validate_reasoning(cls, v):
        """Validate and sanitize reasoning text."""
        return sanitize_text(v)

class RevisionEvaluation(BaseModel):
    approved: bool = Field(..., description="Whether the revision is approved (True) or rejected (False).")
    reasoning: str = Field(..., description="Reasoning for approval or rejection.")
    impact_assessment: str = Field(..., description="Assessment of how this revision impacts each criterion.")
    
    @field_validator('reasoning')
    def validate_reasoning(cls, v):
        """Validate and sanitize reasoning text."""
        return sanitize_text(v)
    
    @field_validator('impact_assessment')
    def validate_impact(cls, v):
        """Validate and sanitize impact assessment text."""
        return sanitize_text(v)

class ItemDetail(BaseModel):
    item_title: str = Field(..., description="Title of the plan item")
    original_evaluation: Dict[str, str] = Field(..., description="Summary of original evaluations for this item")
    revision_request: Optional[RevisionRequest] = Field(None, description="The requested revision if any")
    revision_evaluation: Optional[RevisionEvaluation] = Field(None, description="Evaluation of the revision request")

# --- Module 4 Output ---
class Module4Output(BaseModel):
    goal: str
    selected_criteria: list[SuccessCriteria]
    selected_outline: PlanOutline  # Original outline
    expanded_outline: PlanOutline  # Expanded items with full descriptions
    evaluation_results: list[EvalResult]  # Original evaluation results
    item_details: list[ItemDetail]  # Details about each item's revision
    criteria_coverage_summary: Dict[str, Dict[str, int]] = Field(
        default_factory=dict, description="Summary of original vs post-revision criteria coverage"
    )

# --- Agents ---
criteria_assessment_agent = Agent(
    name="CriteriaAssessor",
    instructions=(
        "You are a criteria assessment expert. Given a goal, multiple success criteria, "
        "an expanded plan item, and evaluation results, analyze if any revisions are needed "
        "to better address one or more of the criteria that weren't fully met. "
        "Your assessment should be comprehensive, considering how each criteria is or isn't addressed "
        "by the current item description. "
        "Provide a detailed reasoning about why specific revisions would help meet the criteria better. "
        "If no revision is needed, return an empty string."
    ),
    model="gpt-4o",
    output_type=str,
    hooks=logging_hooks,
)

request_revision_agent = Agent(
    name="RevisionRequester",
    instructions=(
        "You are a plan improvement specialist. Given a goal, multiple success criteria, "
        "a full plan outline, a detailed expansion of a single plan item, and evaluation results, "
        "identify specific areas where the item could be improved to better address criteria marked as 'fail'. "
        "If all criteria are already marked as 'pass' or if improvements aren't needed, return an empty string. "
        "Otherwise, provide ONE specific, actionable revision request, detailed reasoning, and list the specific "
        "criteria this revision would address. Your revision should always aim to better fulfill the original user goal."
    ),
    model="gpt-4o",
    output_type=RevisionRequest | str,  # Allow empty string output
    hooks=logging_hooks,
)

evaluate_revision_agent = Agent(
    name="RevisionEvaluator",
    instructions=(
        "You are a revision evaluation expert. Given a goal, multiple success criteria, "
        "a full plan outline, an expanded plan item, and a suggested revision, "
        "evaluate whether the revision would significantly improve the item's ability to address "
        "the success criteria, particularly any that were previously not met. "
        "Provide your assessment of how the revision would impact each criterion (improve, worsen, or no change) "
        "as a single string in the impact_assessment field. Format your assessment as a list with each criterion "
        "on a new line.\n\n"
        "Output your approval decision (True/False) and detailed reasoning."
    ),
    model="gpt-4o",
    output_type=RevisionEvaluation,
    hooks=logging_hooks,
)

async def validate_module4_output(
    agent: Agent, agent_output: Any, context: RunContextWrapper[None]
) -> GuardrailFunctionOutput:
    """Validates the output of Module 4."""
    try:
        logger.info("Validating Module 4 output...")
        # Log only a truncated version of the output to avoid excessive logging
        truncated_output = {k: v for k, v in agent_output.model_dump().items() if k not in ['expanded_outline', 'evaluation_results']}
        logger.info(f"Output to validate (truncated): {json.dumps(truncated_output, indent=2)}")
        Module4Output.model_validate(agent_output)
        logger.info("Module 4 output validation passed")
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)
    except ValidationError as e:
        logger.error(f"Module 4 output validation failed: {e}")
        return GuardrailFunctionOutput(
            output_info={"error": str(e)}, tripwire_triggered=True
        )

def get_original_evaluation_summary(
    item_title: str, 
    evaluation_results: List[EvalResult]
) -> Dict[str, str]:
    """Create a summary of original evaluation results for a specific item."""
    summary = {}
    
    for result in evaluation_results:
        criterion = result.criteria.criteria
        # Find results that mention this item in their reasoning
        if item_title.lower() in result.reasoning.lower():
            summary[criterion] = result.result
    
    # If no results were found, assume this is a general result without item specifics
    if not summary:
        for result in evaluation_results:
            criterion = result.criteria.criteria
            if criterion not in summary:
                summary[criterion] = "unknown"
    
    return summary

async def process_item_for_revision(
    goal: str,
    selected_criteria: List[SuccessCriteria],
    selected_outline: PlanOutline,
    expanded_outline: PlanOutline,
    evaluation_results: List[EvalResult],
    item_index: int,
    context: RunContextWrapper[None],
) -> ItemDetail:
    """Process an item for potential revision based on evaluation results."""
    item = expanded_outline.plan_items[item_index]
    item_title = item.item_title
    item_description = item.item_description
    
    logger.info(f"Processing item for revision: {item_title}")
    
    # Format criteria for input
    criteria_json = json.dumps([c.model_dump() for c in selected_criteria], indent=2)
    
    # Get item-specific evaluation results
    item_evaluations = []
    for result in evaluation_results:
        if item_title.lower() in result.reasoning.lower():
            item_evaluations.append(result)
    
    # Create a summary of the original evaluations
    original_evaluation = get_original_evaluation_summary(item_title, evaluation_results)
    logger.info(f"Original evaluation summary for {item_title}: {json.dumps(original_evaluation, indent=2)}")
    
    # Prepare failed criteria info
    failed_criteria = [eval_result for eval_result in evaluation_results 
                      if eval_result.result == "fail" and item_title.lower() in eval_result.reasoning.lower()]
    
    # Check if any criteria failed for this item
    if not failed_criteria:
        logger.info(f"No failed criteria for {item_title}, skipping revision request")
        return ItemDetail(
            item_title=item_title,
            original_evaluation=original_evaluation,
            revision_request=None,
            revision_evaluation=None
        )
    
    # Format evaluation results for input
    evaluations_text = "\n\n".join([
        f"Criterion: {eval_result.criteria.criteria}\n"
        f"Result: {eval_result.result}\n"
        f"Reasoning: {eval_result.reasoning}"
        for eval_result in evaluation_results 
        if item_title.lower() in eval_result.reasoning.lower()
    ])
    
    # Create input for revision requester
    revision_input = (
        f"Goal: {goal}\n\n"
        f"Success Criteria:\n{criteria_json}\n\n"
        f"Plan Item Title: {item_title}\n\n"
        f"Plan Item Description:\n{item_description}\n\n"
        f"Evaluation Results:\n{evaluations_text}\n\n"
        f"Based on the above, identify ONE specific revision that would help this item "
        f"better address the failed criteria. Be specific and actionable. "
        f"If no revision is needed, return an empty string."
    )
    
    logger.info(f"Revision request input for {item_title} (first 5000 chars): {revision_input[:5000]}...")
    
    # Get revision request
    revision_result = await Runner.run(
        request_revision_agent,
        input=revision_input,
        context=context,
    )
    
    revision_request = revision_result.final_output
    
    # If empty string response or not a RevisionRequest object, no revision needed
    if isinstance(revision_request, str) and not revision_request:
        logger.info(f"No revision requested for {item_title}")
        return ItemDetail(
            item_title=item_title,
            original_evaluation=original_evaluation,
            revision_request=None,
            revision_evaluation=None
        )
    
    logger.info(f"Revision requested for {item_title}: {revision_request.revision_request_content[:200]}...")
    
    # Create input for evaluation
    evaluation_input = (
        f"Goal: {goal}\n\n"
        f"Success Criteria:\n{criteria_json}\n\n"
        f"Plan Item Title: {item_title}\n\n"
        f"Original Plan Item Description:\n{item_description}\n\n"
        f"Revision Request:\n{revision_request.revision_request_content}\n\n"
        f"Revision Reasoning:\n{revision_request.reasoning}\n\n"
        f"Targeted Criteria:\n{', '.join(revision_request.targeted_criteria)}\n\n"
        f"Evaluate whether this revision would significantly improve the item's ability to "
        f"address the success criteria, particularly those that weren't fully met before. "
        f"For each criterion, assess how the revision would impact it (improve, worsen, or no change). "
        f"Format your impact assessment as a list with each criterion on its own line."
    )
    
    logger.info(f"Revision evaluation input for {item_title} (first 5000 chars): {evaluation_input[:5000]}...")
    
    # Evaluate the revision
    evaluation_result = await Runner.run(
        evaluate_revision_agent,
        input=evaluation_input,
        context=context,
    )
    
    revision_evaluation = evaluation_result.final_output
    logger.info(f"Revision evaluation for {item_title}: {revision_evaluation.approved} - {revision_evaluation.reasoning[:200]}...")
    logger.info(f"Impact assessment for {item_title}: {revision_evaluation.impact_assessment}")
    
    # Return the item detail with revision information
    return ItemDetail(
        item_title=item_title,
        original_evaluation=original_evaluation,
        revision_request=revision_request,
        revision_evaluation=revision_evaluation
    )

def generate_criteria_coverage_summary(
    original_evaluation_results: List[EvalResult],
    item_details: List[ItemDetail]
) -> Dict[str, Dict[str, int]]:
    """Generate a summary of criteria coverage before and after revisions."""
    # Initialize summary dictionary
    summary = {}
    
    # Get unique criteria
    all_criteria = set(result.criteria.criteria for result in original_evaluation_results)
    
    # Count original pass/fail
    for criterion in all_criteria:
        summary[criterion] = {
            "original_pass": sum(1 for r in original_evaluation_results if r.criteria.criteria == criterion and r.result == "pass"),
            "original_fail": sum(1 for r in original_evaluation_results if r.criteria.criteria == criterion and r.result == "fail"),
            "estimated_improvements": 0
        }
    
    # Count estimated improvements from approved revisions
    for item in item_details:
        if item.revision_request and item.revision_evaluation and item.revision_evaluation.approved:
            # Parse the impact assessment string to find improvements
            impact_text = item.revision_evaluation.impact_assessment.lower()
            
            for criterion in all_criteria:
                criterion_lower = criterion.lower()
                if criterion_lower in impact_text and any(term in impact_text for term in ["improve", "improves", "improved", "enhancement", "better", "enhance", "positive"]):
                    if criterion in summary:
                        summary[criterion]["estimated_improvements"] += 1
    
    return summary

async def run_module_4(input_file: str, output_file: str) -> None:
    """Runs Module 4."""
    context = RunContextWrapper(context=None)

    try:
        logger.info(f"Starting Module 4, reading input from {input_file}")
        with open(input_file, "r") as f:
            module_3_data = json.load(f)
            logger.info(f"Successfully loaded data from {input_file}")

        # Convert to Pydantic objects
        module_3_output = Module3Output.model_validate(module_3_data)
        goal = module_3_output.goal
        selected_criteria = module_3_output.selected_criteria
        selected_outline = module_3_output.selected_outline
        expanded_outline = module_3_output.expanded_outline
        evaluation_results = module_3_output.evaluation_results
        
        logger.info(f"Goal: {goal}")
        logger.info(f"Number of selected criteria: {len(selected_criteria)}")
        for i, criterion in enumerate(selected_criteria):
            logger.info(f"Criterion {i+1}: {criterion.criteria}")
        
        logger.info(f"Original Criteria Summary: {json.dumps(module_3_output.criteria_summary, indent=2)}")
        
        # Process each item sequentially for revision
        logger.info("Processing items for potential revisions...")
        item_details = []
        
        for i, item in enumerate(expanded_outline.plan_items):
            item_detail = await process_item_for_revision(
                goal=goal,
                selected_criteria=selected_criteria,
                selected_outline=selected_outline,
                expanded_outline=expanded_outline,
                evaluation_results=evaluation_results,
                item_index=i,
                context=context,
            )
            
            item_details.append(item_detail)
            logger.info(f"Completed processing item {i+1}: {item.item_title}")
        
        # Generate criteria coverage summary
        criteria_coverage = generate_criteria_coverage_summary(evaluation_results, item_details)
        logger.info(f"Criteria coverage summary: {json.dumps(criteria_coverage, indent=2)}")
        
        # Create the output object
        logger.info("Creating Module 4 output object")
        module_4_output = Module4Output(
            goal=goal,
            selected_criteria=selected_criteria,
            selected_outline=selected_outline,
            expanded_outline=expanded_outline,
            evaluation_results=evaluation_results,
            item_details=item_details,
            criteria_coverage_summary=criteria_coverage,
        )

        # Apply guardrail
        logger.info("Applying output guardrail...")
        guardrail = OutputGuardrail(guardrail_function=validate_module4_output)
        guardrail_result = await guardrail.run(
            agent=evaluate_revision_agent,
            agent_output=module_4_output,
            context=context,
        )
        
        if guardrail_result.output.tripwire_triggered:
            logger.error(f"Guardrail failed: {guardrail_result.output.output_info}")
            return  # Exit if validation fails

        # --- Smart JSON Export ---
        # Create data directory if it doesn't exist
        output_dir = os.path.dirname(output_file)
        os.makedirs(output_dir, exist_ok=True)
        
        # Create timestamped version
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = os.path.basename(output_file)
        name, ext = os.path.splitext(filename)
        timestamped_file = os.path.join(output_dir, f"{name}_{timestamp}{ext}")
        
        # Export both versions
        with open(output_file, "w") as f:
            json.dump(module_4_output.model_dump(), f, indent=4)
        with open(timestamped_file, "w") as f:
            json.dump(module_4_output.model_dump(), f, indent=4)
        
        logger.info(f"Module 4 completed. Output saved to {output_file}")
        logger.info(f"Timestamped output saved to {timestamped_file}")

    except Exception as e:
        logger.error(f"An error occurred in Module 4: {e}")
        import traceback
        logger.error(traceback.format_exc())  # Log the full stack trace

async def main():
    logger.info("Starting main function")
    input_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    input_file = os.path.join(input_dir, "module3_output.json")
    output_file = os.path.join(input_dir, "module4_output.json")
    await run_module_4(input_file, output_file)
    logger.info("Main function completed")

if __name__ == "__main__":
    logger.info("Module 4 script starting")
    asyncio.run(main())
    logger.info("Module 4 script completed")