import json
from datetime import datetime

from fastapi import APIRouter
from fastapi import HTTPException, Depends ,Security
from fastapi_jwt_auth import AuthJWT
from fastapi_sqlalchemy import db
from pydantic import BaseModel

from jsonmerge import merge
from pytz import timezone
from sqlalchemy import func, or_
from superagi.models.agent_execution_permission import AgentExecutionPermission
from superagi.worker import execute_agent
from superagi.helper.auth import check_auth,validate_api_key,get_organisation_from_api_key
from superagi.models.agent import Agent
from superagi.models.agent_execution_config import AgentExecutionConfiguration
from superagi.models.agent_config import AgentConfiguration
from superagi.models.agent_schedule import AgentSchedule
from superagi.models.agent_template import AgentTemplate
from superagi.models.project import Project
from superagi.models.workflows.agent_workflow import AgentWorkflow
from superagi.models.agent_execution import AgentExecution
from superagi.models.tool import Tool
from superagi.models.api_key import ApiKey
from superagi.models.organisation import Organisation
from superagi.models.resource import Resource
from superagi.controllers.types.agent_schedule import AgentScheduleInput
from superagi.controllers.types.agent_with_config import AgentConfigInput
from superagi.controllers.types.agent_with_config_schedule import AgentConfigSchedule
from superagi.controllers.types.agent_with_config import AgentConfigExtInput,AgentConfigUpdateExtInput
from superagi.models.workflows.iteration_workflow import IterationWorkflow
from superagi.helper.s3_helper import S3Helper
from jsonmerge import merge
from datetime import datetime
import json
from typing import Optional,List
import pytz
import boto3
from superagi.config.config import get_config
from superagi.models.toolkit import Toolkit
from superagi.models.knowledges import Knowledges

from sqlalchemy import func
from superagi.helper.auth import check_auth, get_user_organisation
from superagi.apm.event_handler import EventHandler

router = APIRouter()

class AgentExecutionIn(BaseModel):
    name: Optional[str]
    goal: Optional[List[str]]
    instruction: Optional[List[str]]

    class Config:
        orm_mode = True

class RunFilterConfigIn(BaseModel):
    run_ids:Optional[List[int]]
    run_status_filter:Optional[str]

    class Config:
        orm_mode = True

class ExecutionStateChangeConfigIn(BaseModel):
    run_ids:Optional[List[int]]

    class Config:
        orm_mode = True

class RunIDConfig(BaseModel):
    run_ids:List[int]

    class Config:
        orm_mode = True

@router.post("",status_code=200)
def create_agent_with_config(agent_with_config: AgentConfigExtInput,
                             api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):

    project=Project.get_project_from_org_id(db.session,organisation.id)
    tools_arr=Toolkit.get_tool_and_toolkit_arr(db.session,agent_with_config.tools)
    invalid_tools = Tool.get_invalid_tools(tools_arr, db.session)
    if len(invalid_tools) > 0:  # If the returned value is not True (then it is an invalid tool_id)
        raise HTTPException(status_code=404,
                            detail=f"Tool with IDs {str(invalid_tools)} does not exist. 404 Not Found.")
    
    agent_with_config.tools=tools_arr
    agent_with_config.project_id=project.id
    agent_with_config.exit="No exit criterion"
    agent_with_config.permission_type="God Mode"
    agent_with_config.LTM_DB=None

    db_agent = Agent.create_agent_with_config(db, agent_with_config)

    if agent_with_config.schedule is not None:
        agent_schedule = AgentSchedule.get_schedule_from_config(db.session,db_agent,agent_with_config.schedule)
        if agent_schedule is None:
            raise HTTPException(status_code=500, detail="Failed to schedule agent")
            
        EventHandler(session=db.session).create_event('agent_created', {'agent_name': agent_with_config.name,
                                                                            'model': agent_with_config.model}, db_agent.id,
                                                        organisation.id if organisation else 0)
        db.session.commit()
        return {
            "agent_id":db_agent.id
        }
    
    start_step = AgentWorkflow.fetch_trigger_step_id(db.session, db_agent.agent_workflow_id)
    iteration_step_id = IterationWorkflow.fetch_trigger_step_id(db.session,
                                                                start_step.action_reference_id).id if start_step.action_type == "ITERATION_WORKFLOW" else -1
    # Creating an execution with RUNNING status
    execution = AgentExecution(status='CREATED', last_execution_time=datetime.now(), agent_id=db_agent.id,
                               name="New Run", current_agent_step_id=start_step.id, iteration_workflow_step_id=iteration_step_id)
    agent_execution_configs = {
        "goal": agent_with_config.goal,
        "instruction": agent_with_config.instruction
    }
    db.session.add(execution)
    db.session.commit()
    db.session.flush()
    AgentExecutionConfiguration.add_or_update_agent_execution_config(session=db.session, execution=execution,
                                                                     agent_execution_configs=agent_execution_configs)

    organisation = db_agent.get_agent_organisation(db.session)
    EventHandler(session=db.session).create_event('agent_created', {'agent_name': agent_with_config.name,
                                                                    'model': agent_with_config.model}, db_agent.id,
                                                  organisation.id if organisation else 0)
    # execute_agent.delay(execution.id, datetime.now())
    db.session.commit()
    return {
        "agent_id":db_agent.id
    }

@router.post("/run/{agent_id}",status_code=200)
def create_run(agent_id:int,agent_execution: AgentExecutionIn,api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):
    agent=Agent.get_agent_from_id(db.session,agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    project=Project.get_project_from_id(db.session,agent.project_id)
    if project.organisation_id!=organisation.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    db_schedule=AgentSchedule.get_schedule_from_agent_id(db.session,agent_id)
    if db_schedule is not None:
        raise HTTPException(status_code=409, detail="Agent is already scheduled,cannot run")
    
    start_step_id = AgentWorkflow.fetch_trigger_step_id(db.session, agent.agent_workflow_id)
    db_agent_execution=AgentExecution.get_execution_from_agent_id_with_status(db.session,agent_id,"CREATED")

    if db_agent_execution is None:
        db_agent_execution = AgentExecution(status="RUNNING", last_execution_time=datetime.now(),
                                            agent_id=agent_id, name=agent_execution.name, num_of_calls=0,
                                            num_of_tokens=0,
                                            current_step_id=start_step_id)
        db.session.add(db_agent_execution)
    else:
        db_agent_execution.status = "RUNNING"

    db.session.commit()
    db.session.flush()
    agent_execution_configs=AgentExecution.get_updated_execution_config_obj(agent_execution)
    
    if agent_execution_configs != {}:
        AgentExecutionConfiguration.add_or_update_agent_execution_config(session=db.session, execution=db_agent_execution,
                                                                     agent_execution_configs=agent_execution_configs)
    EventHandler(session=db.session).create_event('run_created', {'agent_execution_id': db_agent_execution.id,'agent_execution_name':db_agent_execution.name},
                                 agent_id, organisation.id if organisation else 0)

    if db_agent_execution.status == "RUNNING":
      execute_agent.delay(db_agent_execution.id, datetime.now())
    return {
        "run_id":db_agent_execution.id
    }

@router.put("/update/{agent_id}")
def update_agent(agent_id: int, agent_with_config: AgentConfigUpdateExtInput,api_key: str = Security(validate_api_key),
                                        organisation:Organisation = Depends(get_organisation_from_api_key)):
    
    db_agent=Agent.get_agent_from_id_with_filter(db.session,agent_id,False)
    if not db_agent:
        raise HTTPException(status_code=404, detail="agent not found")
    
    project=Project.get_project_from_id(db.session,db_agent.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    
    if project.organisation_id!=organisation.id:
        raise HTTPException(status_code=404, detail="Agent not found")

    db_execution=AgentExecution.get_execution_from_agent_id_with_status(db.session,agent_id,"RUNNING")
    if db_execution is not None:
        raise HTTPException(status_code=409, detail="Agent is already running,please pause and then update")
     
    db_schedule=AgentSchedule.get_schedule_from_agent_id(db.session,agent_id)
    if db_schedule is not None:
        raise HTTPException(status_code=409, detail="Agent is already scheduled,cannot update")
     
    tools_arr=Toolkit.get_tool_and_toolkit_arr(db.session,agent_with_config.tools)
    invalid_tools = Tool.get_invalid_tools(tools_arr, db.session)
    if len(invalid_tools) > 0:  # If the returned value is not True (then it is an invalid tool_id)
        raise HTTPException(status_code=404,
                            detail=f"Tool with IDs {str(invalid_tools)} does not exist.")
    
    agent_with_config.tools=tools_arr
    agent_with_config.project_id=project.id
    agent_with_config.exit="No exit criterion"
    agent_with_config.permission_type="God Mode"
    agent_with_config.LTM_DB=None

    for key,value in agent_with_config.dict().items():
        if hasattr(db_agent,key) and value is not None:
            setattr(db_agent,key,value)
        
    db.session.commit()
    db.session.flush()

    start_step = AgentWorkflow.fetch_trigger_step_id(db.session, db_agent.agent_workflow_id)
    iteration_step_id = IterationWorkflow.fetch_trigger_step_id(db.session,
                                                                start_step.action_reference_id).id if start_step.action_type == "ITERATION_WORKFLOW" else -1
    execution = AgentExecution(status='CREATED', last_execution_time=datetime.now(), agent_id=db_agent.id,
                               name="New Run", current_agent_step_id=start_step.id, iteration_workflow_step_id=iteration_step_id)
    agent_execution_configs = {
        "goal": agent_with_config.goal,
        "instruction": agent_with_config.instruction
    }
    db.session.add(execution)
    db.session.commit()
    db.session.flush()
    AgentExecutionConfiguration.add_or_update_agent_execution_config(session=db.session, execution=execution,
                                                                     agent_execution_configs=agent_execution_configs)
    db.session.commit()

    return {
        "agent_id":db_agent.id
    }


@router.get("/run/{agent_id}")
def get_agent_runs(agent_id:int,filter_config:RunFilterConfigIn,api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):
    agent=Agent.get_agent_from_id_with_filter(db.session,agent_id,False)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    project=Project.get_project_from_id(db.session,agent.project_id)
    if project.organisation_id!=organisation.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    db_execution_arr=[]
    if filter_config.run_status_filter is not None:
        filter_config.run_status_filter=filter_config.run_status_filter.upper()

    db_execution_arr=AgentExecution.get_all_executions_with_filter_config_and_agent_id(db.session,agent.id,filter_config)
    
    response_arr=[]
    for ind_execution in db_execution_arr:
        response_arr.append({"run_id":ind_execution.id, "status":ind_execution.status})

    return response_arr


@router.get("/pause/{agent_id}",status_code=200)
def pause_agent_runs(agent_id:int,execution_state_change_input:ExecutionStateChangeConfigIn,api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):
    agent=Agent.get_agent_from_id_with_filter(db.session,agent_id,False)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    project=Project.get_project_from_id(db.session,agent.project_id)
    if project.organisation_id!=organisation.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    db_execution_arr=AgentExecution.get_all_executions_with_status_and_agent_id(db.session,agent.id,execution_state_change_input,"RUNNING")
    for ind_execution in db_execution_arr:
        ind_execution.status="PAUSED"
    db.session.commit()
    db.session.flush()
    return {
        "result":"success"
    }

@router.get("/resume/{agent_id}",status_code=200)
def resume_agent_runs(agent_id:int,execution_state_change_input:ExecutionStateChangeConfigIn,api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):
    agent=Agent.get_agent_from_id_with_filter(db.session,agent_id,False)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    project=Project.get_project_from_id(db.session,agent.project_id)
    if project.organisation_id!=organisation.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    db_execution_arr=AgentExecution.get_all_executions_with_status_and_agent_id(db.session,agent.id,execution_state_change_input,"PAUSED")
    for ind_execution in db_execution_arr:
        ind_execution.status="RUNNING"
        
    db.session.commit()
    db.session.flush()
    return {
        "result":"success"
    }

@router.get("/resources/output",status_code=201)
def get_run_resources(run_id_config:RunIDConfig,api_key: str = Security(validate_api_key),organisation:Organisation = Depends(get_organisation_from_api_key)):
    run_ids_arr=run_id_config.run_ids
    if len(run_ids_arr)==0:  
        raise HTTPException(status_code=404,
                            detail=f"No execution_id found")
    #Checking if the run_ids whose output files are requested belong to the organisation 
    try:
        AgentExecution.validate_run_ids(db.session,run_ids_arr,organisation.id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    
    db_resources_arr=Resource.get_all_resources_from_run_ids(db.session,run_ids_arr)
    response_obj=S3Helper.get_download_url_of_resources(db_resources_arr)
    return response_obj

